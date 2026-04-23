"""Hand Control — Mac server.

Serves the phone UI and handles phone → Mac control events over WebSocket.

Control flow:
    phone hold_start
        → focus selected Cursor window
        → press-and-hold Right Option (Wispr Flow hotkey)

    phone hold_end
        → release Right Option
        → wait for Wispr to finish typing (CGEventTap keystroke watcher)
        → send "transcription_ready" to phone so it enables Submit / Delete

    phone submit        → press Option+Enter (Cursor's "queue message"
                          shortcut — message is appended after the current
                          agent run instead of interrupting it)
    phone delete        → press Cmd+Z (undo Wispr's last insertion)

    phone switch_prev / switch_next / select
        → update the server-side selected window index
        → focus that window immediately so user can see which one is active
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import socket
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional


def _fail_fast_if_wrong_platform() -> None:
    """Hand Control uses AppleScript, CoreGraphics, and CGEventTap — all
    macOS-only. Fail with a clear, friendly message if we're elsewhere,
    rather than letting the user hit a cryptic `ImportError` on pyobjc.
    """
    if platform.system() != "Darwin":
        sys.stderr.write(
            "\nHand Control only runs on macOS.\n"
            "It drives AppleScript, CoreGraphics, and CGEventTap, which are\n"
            f"Apple-only APIs. Current platform: {platform.system()}.\n\n"
        )
        sys.exit(1)


_fail_fast_if_wrong_platform()

# Make stdout / stderr line-buffered so our diagnostic prints (startup
# banner, debug logs, etc.) appear immediately even when the server is
# launched via a wrapper that pipes stdout into a file or terminal tail.
try:
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    sys.stderr.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
except Exception:
    pass

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

# Used for primary-display size + cursor warp. These live in Quartz
# on modern pyobjc, but some shells expose them through different
# submodules — import them best-effort so a stub install doesn't
# take down the whole server.
try:
    from Quartz import (
        CGDisplayBounds,
        CGMainDisplayID,
        CGWarpMouseCursorPosition,
    )
    _QUARTZ_DISPLAY_OK = True
except Exception:  # pragma: no cover
    _QUARTZ_DISPLAY_OK = False

from .certs import ensure_cert
from .cursor_windows import CursorWindow, focus_window, list_windows
from .mouse_control import mouse_click, mouse_move_by, mouse_scroll
from .key_control import (
    press_cmd_l,
    press_cmd_z,
    press_enter,
    press_option_enter,
    right_option_down,
    right_option_up,
    type_string,
)
from .keystroke_watcher import KeystrokeWatcher
from .peer import Peer, PeerWindow
from .presets import Preset, load_presets
from .virtual_cursor import ScreenLayout, VirtualCursor

PHONE_DIR = Path(__file__).resolve().parent.parent / "phone"
POLL_INTERVAL_S = 1.0

# Cursor keyboard behavior:
#   Enter         → submit (may interrupt current agent run)
#   Option+Enter  → queue message to run after current task finishes
#   Cmd+Enter     → "stop & send" (explicitly interrupt)
#
# Set to True to always queue. If the agent is idle, Option+Enter just
# submits normally, so this is the safe default for agent workflows.
QUEUE_INSTEAD_OF_INTERRUPT = True


# Auto-focus Cursor's chat input after every window-focus. Fires
# Cmd+L on Mac / Ctrl+L on the PC peer. The phone's swipe ends up
# on a ready-to-dictate text field instead of (often) the code
# editor. Set ``HC_AUTO_FOCUS_CHAT=0`` in the env to disable.
_AUTO_FOCUS_CHAT = os.environ.get("HC_AUTO_FOCUS_CHAT", "1").strip() != "0"
# Delay between raising the window and firing the hotkey. macOS
# propagates frontmost-app changes asynchronously; without this the
# hotkey can race ahead to whatever was frontmost BEFORE Cursor.
# Override with HC_AUTO_FOCUS_CHAT_DELAY_MS.
_AUTO_FOCUS_CHAT_DELAY_S = (
    max(0, int(os.environ.get("HC_AUTO_FOCUS_CHAT_DELAY_MS", "120").strip()))
    / 1000.0
)


def _sort_key(w: CursorWindow) -> tuple[str, str]:
    """Stable sort: alphabetical by project (then title) so box positions
    on the phone don't shuffle every time we focus a window."""
    return (w.project.lower(), w.title.lower())


class State:
    def __init__(self) -> None:
        self.windows: list[CursorWindow] = []
        # Track the selected window by title (identity), not index — macOS
        # reorders the window list whenever we focus something.
        self.selected_title: Optional[str] = None
        self.selected_host: str = "mac"  # 'mac' | 'pc'
        self.clients: set[WebSocket] = set()
        self.watcher = KeystrokeWatcher()
        self.lock = asyncio.Lock()
        self.hold_start_ts: Optional[float] = None
        # Presets are loaded once at startup. Users who edit
        # presets.json while the server is running can restart to pick
        # up changes.
        self.presets: list[Preset] = load_presets()
        self._presets_by_id: dict[str, Preset] = {p.id: p for p in self.presets}
        # Peer (Windows PC) — created at startup if HC_PEER_URL is set.
        self.peer: Optional[Peer] = None
        # Virtual cursor — initialized in lifespan() once we know both
        # screens' sizes. None until then.
        self.vcur: Optional[VirtualCursor] = None

    def preset(self, preset_id: str) -> Optional[Preset]:
        return self._presets_by_id.get(preset_id)

    def _all_windows(self) -> list[dict]:
        """Unified list of Cursor windows from Mac + (optionally) PC.

        Mac windows come first, then PC windows alphabetical. Each
        entry is a small dict that matches what the phone expects:
        ``{title, project, host}``. We derive project from title on
        the PC side (same as Mac's AppleScript does).
        """
        out: list[dict] = []
        for w in self.windows:
            out.append({"title": w.title, "project": w.project, "host": "mac"})
        if self.peer and self.peer.state.healthy:
            for pw in self.peer.state.windows:
                out.append(
                    {
                        "title": pw.title,
                        "project": _project_from_title(pw.title),
                        "host": "pc",
                    }
                )
        return out

    def _selected_index(self) -> int:
        """Index of the currently-selected card in the unified deck
        (Mac windows then PC windows)."""
        all_w = self._all_windows()
        if not all_w:
            return -1
        if self.selected_title is not None:
            for i, w in enumerate(all_w):
                if w["title"] == self.selected_title and w["host"] == self.selected_host:
                    return i
        return 0

    def selected_window(self) -> Optional[dict]:
        """Return the currently-selected card as a dict with host info."""
        idx = self._selected_index()
        if idx < 0:
            return None
        return self._all_windows()[idx]

    def to_payload(self) -> dict:
        peer_info = None
        if self.peer and self.peer.state.enabled:
            peer_info = {
                "configured": True,
                "healthy": self.peer.state.healthy,
                "hostname": self.peer.state.hostname,
                "side": self.peer.state.side,
            }
        return {
            "type": "state",
            "windows": self._all_windows(),
            "selected": self._selected_index(),
            # Send only the public-safe view (id, label, submit mode) —
            # the actual prompt text stays server-side.
            "presets": [p.to_public_dict() for p in self.presets],
            "peer": peer_info,
            "cursor_host": self.vcur.host if self.vcur else "mac",
        }


def _project_from_title(title: str) -> str:
    """Heuristic: Cursor's window title is usually ``"file - project -
    Cursor"``. Grab the middle segment; fall back to the whole title
    if the format doesn't match."""
    parts = [p.strip() for p in title.split(" - ")]
    if len(parts) >= 3 and parts[-1].lower() == "cursor":
        return parts[-2]
    return title


state = State()


async def broadcast(payload: dict) -> None:
    message = json.dumps(payload)
    dead: list[WebSocket] = []
    for client in state.clients:
        try:
            await client.send_text(message)
        except Exception:
            dead.append(client)
    for c in dead:
        state.clients.discard(c)


async def poll_windows() -> None:
    """Periodically refresh the list of open Cursor windows."""
    prev_key: tuple = ()
    while True:
        try:
            windows = list_windows()
        except Exception as exc:
            print(f"[poll_windows] error: {exc}")
            windows = []

        # Stable order so phone box positions don't shuffle as z-order changes.
        windows.sort(key=_sort_key)

        key = tuple((w.title, w.project) for w in windows)
        async with state.lock:
            if key != prev_key:
                state.windows = windows
                # If the previously selected window went away, fall back to
                # the first available. Otherwise the selection sticks with
                # the same window by title.
                titles = {w.title for w in windows}
                if state.selected_title not in titles:
                    state.selected_title = windows[0].title if windows else None
                prev_key = key
                print(
                    f"[windows] updated ({len(windows)}): "
                    + ", ".join(f"{i}={w.project}" for i, w in enumerate(windows))
                )
                await broadcast(state.to_payload())
        await asyncio.sleep(POLL_INTERVAL_S)


# --- Trackpad: virtual cursor dispatch --------------------------------------
#
# The phone always sends raw (dx, dy) deltas; we decide here whether
# each delta affects the Mac cursor or the PC cursor based on the
# virtual cursor position relative to the configured screen layout.


async def _broadcast_cursor_host(host: str) -> None:
    """Notify the phone that the cursor is now on `host` so the pad
    UI can show the right label/accent color."""
    await broadcast({"type": "cursor_host", "host": host})


async def _dispatch_mouse_move(dx: float, dy: float) -> None:
    vcur = state.vcur
    peer = state.peer
    peer_ok = bool(peer and peer.state.healthy)

    # Single-host mode: no peer, just move the Mac cursor directly.
    if vcur is None or not peer_ok or vcur.layout.pc_w == 0:
        try:
            mouse_move_by(dx, dy)
        except Exception as exc:
            print(f"[mouse_move] error: {exc}")
        return

    prev_host = vcur.host
    new_host, local_x, local_y = vcur.apply_delta(dx, dy)
    crossed = new_host != prev_host

    if new_host == "mac":
        if crossed:
            # Just came back from PC — warp the native cursor to the
            # correct Mac-side edge so it doesn't jump to wherever
            # we left it before crossing.
            ex, ey = vcur.mac_edge_on_cross_from_pc()
            if _QUARTZ_DISPLAY_OK:
                try:
                    CGWarpMouseCursorPosition((ex, ey))
                except Exception as exc:
                    print(f"[vcur] mac warp failed: {exc}")
            asyncio.create_task(_broadcast_cursor_host("mac"))
        else:
            try:
                mouse_move_by(dx, dy)
            except Exception as exc:
                print(f"[mouse_move] error: {exc}")
    else:  # new_host == "pc"
        if crossed:
            ex, ey = vcur.pc_edge_on_cross_from_mac()
            # Warp PC cursor so it picks up at the matching edge row.
            asyncio.create_task(peer.warp_cursor(ex, ey))  # type: ignore[union-attr]
            # Also snug the Mac native cursor right to the edge so it
            # doesn't visibly sit mid-screen during the crossing.
            if _QUARTZ_DISPLAY_OK:
                mx0, my0, mx1, my1 = vcur.layout.mac_box()
                if vcur.layout.side == "left":
                    edge_x = 0
                elif vcur.layout.side == "right":
                    edge_x = mx1 - mx0 - 1
                elif vcur.layout.side == "above":
                    edge_x = int(max(0, min(mx1 - mx0 - 1, vcur.x - mx0)))
                else:
                    edge_x = int(max(0, min(mx1 - mx0 - 1, vcur.x - mx0)))
                if vcur.layout.side in ("left", "right"):
                    edge_y = int(max(0, min(my1 - my0 - 1, vcur.y - my0)))
                elif vcur.layout.side == "above":
                    edge_y = 0
                else:
                    edge_y = my1 - my0 - 1
                try:
                    CGWarpMouseCursorPosition((edge_x, edge_y))
                except Exception:
                    pass
            asyncio.create_task(_broadcast_cursor_host("pc"))
        else:
            # Fast-path: forward delta to PC. Fire-and-forget.
            asyncio.create_task(peer.mouse_move(dx, dy))  # type: ignore[union-attr]


async def _dispatch_mouse_click(button: str) -> None:
    vcur = state.vcur
    peer = state.peer
    if vcur and vcur.host == "pc" and peer and peer.state.healthy:
        await peer.mouse_click(button)
    else:
        await asyncio.to_thread(mouse_click, button)


async def _dispatch_mouse_scroll(dx: float, dy: float) -> None:
    vcur = state.vcur
    peer = state.peer
    if vcur and vcur.host == "pc" and peer and peer.state.healthy:
        await peer.mouse_scroll(dx, dy)
    else:
        try:
            mouse_scroll(dy, dx)
        except Exception as exc:
            print(f"[mouse_scroll] error: {exc}")


async def handle_hold_start() -> None:
    state.hold_start_ts = time.monotonic()
    win = state.selected_window()
    if win is None:
        # No window selected — still fire the modifier so Wispr
        # works regardless of focus state.
        right_option_down()
        return

    if win["host"] == "pc" and state.peer and state.peer.state.healthy:
        await state.peer.hold_start(title=win["title"])
    else:
        focus_window(win["title"])
        # Give the WM a beat before pressing Right Option so Wispr
        # activates against the intended window.
        await asyncio.sleep(0.08)
        right_option_down()


async def handle_hold_end() -> None:
    win = state.selected_window()
    on_pc = bool(
        win and win["host"] == "pc" and state.peer and state.peer.state.healthy
    )

    if on_pc:
        auto_submitted = await state.peer.hold_end()  # type: ignore[union-attr]
    else:
        right_option_up()
        release_ts = time.monotonic()
        hold_duration = (
            release_ts - state.hold_start_ts if state.hold_start_ts else 0.0
        )
        await asyncio.to_thread(
            state.watcher.wait_for_typing_to_settle,
            release_ts,
            hold_duration,
        )
        auto_submitted = state.watcher.saw_return_since(release_ts)

    await broadcast(
        {
            "type": "transcription_ready",
            "auto_submitted": auto_submitted,
        }
    )


async def handle_submit() -> None:
    win = state.selected_window()
    if win and win["host"] == "pc" and state.peer and state.peer.state.healthy:
        await state.peer.submit()
        return
    if QUEUE_INSTEAD_OF_INTERRUPT:
        press_option_enter()
    else:
        press_enter()


async def handle_delete() -> None:
    win = state.selected_window()
    if win and win["host"] == "pc" and state.peer and state.peer.state.healthy:
        await state.peer.delete()
        return
    press_cmd_z()


async def handle_preset(preset_id: str) -> None:
    """One-tap preset: focus the selected Cursor window, type the canned
    prompt into its focused input, then submit / queue / do nothing per
    the preset's ``submit`` mode."""
    preset = state.preset(preset_id)
    if preset is None:
        print(f"[preset] unknown id: {preset_id!r}")
        return

    win = state.selected_window()
    if win is None:
        print(f"[preset] no selected window; ignoring {preset.label!r}")
        await broadcast(
            {
                "type": "preset_result",
                "id": preset.id,
                "ok": False,
                "reason": "no_window",
            }
        )
        return

    on_pc = win["host"] == "pc" and state.peer and state.peer.state.healthy

    if on_pc:
        await state.peer.focus_window(win["title"])  # type: ignore[union-attr]
        await asyncio.sleep(0.12)
        # Chat-focus before typing so the preset goes into the chat
        # input, not the code editor (which frequently has focus).
        if _AUTO_FOCUS_CHAT:
            try:
                await state.peer.focus_chat_input()  # type: ignore[union-attr]
                await asyncio.sleep(0.05)
            except Exception as exc:
                print(f"[preset] chat-focus failed: {exc}")
        await state.peer.type_string(preset.text)  # type: ignore[union-attr]
        await asyncio.sleep(0.05)
        if preset.submit == "queue":
            await state.peer.submit()  # type: ignore[union-attr]
        elif preset.submit == "send":
            await state.peer.press_enter()  # type: ignore[union-attr]
    else:
        focus_window(win["title"])
        # Let the WM actually transfer focus before we start firing keys.
        await asyncio.sleep(0.12)
        if _AUTO_FOCUS_CHAT:
            try:
                await asyncio.to_thread(press_cmd_l)
                await asyncio.sleep(0.05)
            except Exception as exc:
                print(f"[preset] chat-focus failed: {exc}")
        # Typing is blocking (~4ms per char × message length). Run in a
        # worker thread so the event loop stays responsive and other
        # clients (or another preset tap) don't queue up behind it.
        await asyncio.to_thread(type_string, preset.text)
        # Small beat so the app registers all typed chars before we submit.
        await asyncio.sleep(0.05)
        if preset.submit == "queue":
            press_option_enter()
        elif preset.submit == "send":
            press_enter()
        # "none" → just leave the text in the field

    print(
        f"[preset] {preset.label!r} → [{win['host']}] "
        f"window={win['project']!r} submit={preset.submit} "
        f"chars={len(preset.text)}"
    )
    await broadcast(
        {
            "type": "preset_result",
            "id": preset.id,
            "ok": True,
            "submit": preset.submit,
            "window": win["project"],
            "host": win["host"],
        }
    )


async def _focus_selected(win: dict) -> None:
    """Focus a window, routing to the correct host, then fire
    Cursor's "focus chat input" hotkey (Cmd+L / Ctrl+L) so the
    phone's swipe lands on a ready-to-dictate text field.

    Why the sleep between the two steps: ``focus_window`` raises the
    Cursor window, but macOS / Windows propagate the frontmost-app
    change asynchronously. Without a brief wait, the hotkey can
    race ahead and go to whatever was frontmost BEFORE Cursor
    (e.g. Safari, where Cmd+L opens the address bar — obviously
    not what anyone wants). 120ms is enough on every machine I've
    tested and still feels instant.

    Disable by setting ``HC_AUTO_FOCUS_CHAT=0`` in the environment.
    """
    if win["host"] == "pc" and state.peer and state.peer.state.healthy:
        await state.peer.focus_window(win["title"])
    else:
        focus_window(win["title"])

    if not _AUTO_FOCUS_CHAT:
        return

    await asyncio.sleep(_AUTO_FOCUS_CHAT_DELAY_S)
    try:
        if win["host"] == "pc" and state.peer and state.peer.state.healthy:
            await state.peer.focus_chat_input()
        else:
            await asyncio.to_thread(press_cmd_l)
    except Exception as exc:
        # Never let a stray focus-hotkey failure swallow the whole
        # swipe — the window is already focused, which is most of
        # what the user wanted.
        print(f"[focus] chat-input hotkey failed: {exc}")


async def handle_select(index: int) -> None:
    async with state.lock:
        all_w = state._all_windows()
        if 0 <= index < len(all_w):
            win = all_w[index]
            state.selected_title = win["title"]
            state.selected_host = win["host"]
            await broadcast(state.to_payload())
        else:
            win = None
    if win is not None:
        await _focus_selected(win)


async def handle_switch(delta: int) -> None:
    async with state.lock:
        all_w = state._all_windows()
        if not all_w:
            return
        current = state._selected_index()
        if current < 0:
            current = 0
        new_idx = (current + delta) % len(all_w)
        win = all_w[new_idx]
        state.selected_title = win["title"]
        state.selected_host = win["host"]
        print(
            f"[switch] delta={delta:+d} {current} -> {new_idx} "
            f"([{win['host']}] {win['project']})"
        )
        await broadcast(state.to_payload())
    await _focus_selected(win)


def _mac_screen_size() -> tuple[int, int]:
    """Primary Mac display size in points. Falls back to 1920x1080 if
    Quartz isn't available (shouldn't happen on a normal install)."""
    if not _QUARTZ_DISPLAY_OK:
        return (1920, 1080)
    try:
        b = CGDisplayBounds(CGMainDisplayID())
        return (int(b.size.width), int(b.size.height))
    except Exception:
        return (1920, 1080)


async def _on_peer_windows_change() -> None:
    """Called by the Peer when its window list changes; rebroadcasts
    the merged deck so all connected phones update."""
    async with state.lock:
        # If the previously-selected PC window disappeared, fall back.
        all_titles = {
            (w["host"], w["title"]) for w in state._all_windows()
        }
        current = (state.selected_host, state.selected_title or "")
        if current not in all_titles:
            if state._all_windows():
                first = state._all_windows()[0]
                state.selected_title = first["title"]
                state.selected_host = first["host"]
            else:
                state.selected_title = None
                state.selected_host = "mac"
        await broadcast(state.to_payload())


def _init_virtual_cursor(mac_w: int, mac_h: int) -> VirtualCursor:
    """Build the virtual cursor from current configuration.

    Called at startup. If there's no peer yet, we use a 0x0 PC region
    which effectively disables edge crossing — once the peer reports
    its size we rebuild the layout.
    """
    peer = state.peer
    if peer and peer.state.healthy:
        pw, ph = peer.state.screen_w or 1920, peer.state.screen_h or 1080
        side = peer.state.side
    else:
        # Pretend the PC is 0-wide so the layout math degenerates to
        # Mac-only until the peer comes online.
        pw, ph = 0, 0
        side = peer.state.side if peer else "right"
    layout = ScreenLayout(
        mac_w=mac_w, mac_h=mac_h, pc_w=pw, pc_h=ph, side=side  # type: ignore[arg-type]
    )
    return VirtualCursor.centered_on_mac(layout)


@asynccontextmanager
async def lifespan(_: FastAPI):
    state.watcher.start()

    mac_w, mac_h = _mac_screen_size()
    state.peer = Peer.from_env(on_windows_change=_on_peer_windows_change)
    if state.peer:
        print(f"[peer] configured → {state.peer.state.base_url} (side={state.peer.state.side})")
        await state.peer.start()

    state.vcur = _init_virtual_cursor(mac_w, mac_h)

    # Rebuild the virtual-cursor layout once the peer health comes in
    # with real screen dimensions. Simple approach: check once a few
    # seconds after startup.
    async def _refresh_layout() -> None:
        await asyncio.sleep(2.0)
        if state.peer and state.peer.state.healthy:
            state.vcur = _init_virtual_cursor(mac_w, mac_h)
            print(
                f"[vcur] layout updated: mac={mac_w}x{mac_h}, "
                f"pc={state.peer.state.screen_w}x{state.peer.state.screen_h}, "
                f"side={state.peer.state.side}"
            )
            await broadcast(state.to_payload())

    task = asyncio.create_task(poll_windows())
    layout_task = asyncio.create_task(_refresh_layout())
    try:
        yield
    finally:
        task.cancel()
        layout_task.cancel()
        if state.peer:
            await state.peer.stop()


app = FastAPI(lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def index() -> FileResponse:
    return FileResponse(PHONE_DIR / "index.html")


@app.get("/manifest.json")
async def manifest() -> FileResponse:
    return FileResponse(PHONE_DIR / "manifest.json", media_type="application/manifest+json")


@app.get("/apple-touch-icon.png")
async def apple_touch_icon() -> FileResponse:
    return FileResponse(PHONE_DIR / "icon-180.png", media_type="image/png")


@app.get("/apple-touch-icon-precomposed.png")
async def apple_touch_icon_precomposed() -> FileResponse:
    return FileResponse(PHONE_DIR / "icon-180.png", media_type="image/png")


@app.get("/icon-180.png")
async def icon_180() -> FileResponse:
    return FileResponse(PHONE_DIR / "icon-180.png", media_type="image/png")


@app.get("/icon-192.png")
async def icon_192() -> FileResponse:
    return FileResponse(PHONE_DIR / "icon-192.png", media_type="image/png")


@app.get("/icon-512.png")
async def icon_512() -> FileResponse:
    return FileResponse(PHONE_DIR / "icon-512.png", media_type="image/png")


@app.get("/favicon.ico")
async def favicon() -> FileResponse:
    return FileResponse(PHONE_DIR / "icon-192.png", media_type="image/png")


@app.get("/trust.crt")
async def trust_crt() -> FileResponse:
    """Serve the self-signed cert as a downloadable iOS configuration
    profile. The ``application/x-x509-ca-cert`` MIME type is what
    makes Safari on iOS show the "This website is trying to download
    a configuration profile" prompt instead of just downloading a
    random file. After the user taps Allow, iOS takes them straight
    to the Profile Installation screen.

    On Android (Chrome), the same MIME triggers the system Credential
    Storage installer.

    Safe to expose: a cert's public half is, by definition, public.
    Nothing here leaks the private key — that lives in ``certs/server.key``
    and is only used inside the uvicorn process.
    """
    from .certs import CERT_DIR

    cert_path = CERT_DIR / "server.crt"
    if not cert_path.exists():
        # Shouldn't happen because ``main()`` generates the cert
        # before uvicorn starts; guard anyway so a stale/broken state
        # returns a clean 404 instead of an opaque 500.
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Cert not found")
    return FileResponse(
        cert_path,
        media_type="application/x-x509-ca-cert",
        filename="HandControl.crt",
    )


# Minimal HTML walkthrough for installing + trusting the cert. Kept
# inline (not templated into a separate file) so the page works even
# if something in ``phone/`` is broken, and so there's nothing extra
# to ship/serve.
_INSTALL_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="theme-color" content="#000000">
  <title>Install Hand Control cert</title>
  <style>
    :root {
      --bg: #000;
      --panel: #0e0e0e;
      --text: #f0f0f0;
      --muted: #8a8a8a;
      --accent: #f25f4c;
      --accent-soft: rgba(242, 95, 76, 0.35);
      --border: #1e1e1e;
    }
    *, *::before, *::after { box-sizing: border-box; }
    html, body { margin: 0; padding: 0; background: var(--bg); color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI",
                   system-ui, sans-serif; }
    body { padding: max(28px, env(safe-area-inset-top)) max(22px, env(safe-area-inset-right))
                   max(28px, env(safe-area-inset-bottom)) max(22px, env(safe-area-inset-left));
      max-width: 640px; margin: 0 auto; line-height: 1.55; }
    h1 { font-size: 26px; font-weight: 800; letter-spacing: -0.01em;
      margin: 0 0 8px; }
    .lede { font-size: 15px; color: var(--muted); margin-bottom: 28px; }
    .cta {
      display: block; text-align: center; margin: 6px 0 22px;
      padding: 18px 22px; font-size: 15px; font-weight: 700; letter-spacing: 0.06em;
      text-transform: uppercase; text-decoration: none;
      background: var(--accent); color: #000; border-radius: 14px;
      box-shadow: 0 10px 30px rgba(242, 95, 76, 0.25);
      transition: transform 0.12s;
    }
    .cta:active { transform: scale(0.98); }
    ol { padding-left: 20px; margin: 0; }
    ol li { margin: 14px 0; }
    ol li b { color: var(--text); }
    .panel {
      background: var(--panel); border: 1px solid var(--border);
      border-radius: 14px; padding: 18px 20px; margin-bottom: 18px;
    }
    .tag {
      display: inline-block; padding: 2px 8px; font-size: 11px; font-weight: 700;
      letter-spacing: 0.08em; text-transform: uppercase;
      background: rgba(242, 95, 76, 0.12); color: var(--accent);
      border: 1px solid var(--accent-soft); border-radius: 999px;
      margin-right: 8px; vertical-align: 2px;
    }
    code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      background: #181818; padding: 1px 6px; border-radius: 5px; font-size: 13px; }
    a { color: var(--accent); }
    .after {
      margin-top: 26px; padding-top: 18px; border-top: 1px solid var(--border);
      color: var(--muted); font-size: 13px;
    }
  </style>
</head>
<body>
  <h1>Install Hand Control certificate</h1>
  <p class="lede">
    One-time setup so Safari stops showing a "Not Private" warning
    every time you open the remote.
  </p>

  <a class="cta" href="/trust.crt" download>Download cert</a>

  <div class="panel">
    <p><span class="tag">iPhone / iPad</span></p>
    <ol>
      <li>Tap <b>Download cert</b> above. When Safari asks
        <i>"This website is trying to download a configuration
        profile"</i>, tap <b>Allow</b>.</li>
      <li>Open <b>Settings</b> → at the top you'll see
        <b>Profile Downloaded</b>. Tap it.
        (If it's not there: <b>Settings → General → VPN &amp; Device
        Management</b> → <b>Hand Control</b>.)</li>
      <li>Tap <b>Install</b> in the top-right, enter your passcode,
        tap <b>Install</b> again when it warns about the profile
        being unverified, and <b>Done</b>.</li>
      <li>Go to <b>Settings → General → About → Certificate Trust
        Settings</b>. Under "Enable full trust for root certificates",
        toggle <b>Hand Control</b> <b>on</b>. Confirm.</li>
      <li>Reload the Hand Control tab in Safari. No more warning.</li>
    </ol>
  </div>

  <div class="panel">
    <p><span class="tag">Android</span></p>
    <ol>
      <li>Tap <b>Download cert</b> above.</li>
      <li>Open the file (or <b>Settings → Security → Encryption &amp;
        credentials → Install a certificate → CA certificate</b>).</li>
      <li>Accept the warning and install. The site will be trusted
        immediately.</li>
    </ol>
  </div>

  <p class="after">
    The cert is generated locally by your Mac (<code>./certs/server.crt</code>),
    never leaves your machine, and stays valid for 5 years. Reinstall
    only if you rename your Mac (the Bonjour hostname in the cert
    changes).
  </p>
</body>
</html>
"""


@app.get("/install", response_class=HTMLResponse)
async def install_page() -> HTMLResponse:
    """Step-by-step page that walks the user through installing the
    self-signed cert on their phone. Links to ``/trust.crt`` for the
    actual download."""
    return HTMLResponse(content=_INSTALL_HTML)


@app.get("/presets")
async def presets_endpoint() -> dict:
    """Inspect the loaded presets (handy when debugging a custom
    ``presets.json``). Includes ``text`` for local debugging since the
    server only listens on the LAN."""
    return {
        "count": len(state.presets),
        "presets": [
            {
                "id": p.id,
                "label": p.label,
                "text": p.text,
                "submit": p.submit,
            }
            for p in state.presets
        ],
    }


app.mount("/static", StaticFiles(directory=str(PHONE_DIR)), name="static")


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    state.clients.add(websocket)
    try:
        await websocket.send_text(json.dumps(state.to_payload()))
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = msg.get("type")
            if kind == "hold_start":
                await handle_hold_start()
            elif kind == "hold_end":
                await handle_hold_end()
            elif kind == "submit":
                await handle_submit()
            elif kind == "delete":
                await handle_delete()
            elif kind == "switch_prev":
                await handle_switch(-1)
            elif kind == "switch_next":
                await handle_switch(+1)
            elif kind == "select":
                idx = msg.get("index")
                if isinstance(idx, int):
                    await handle_select(idx)
            elif kind == "preset":
                pid = msg.get("id")
                if isinstance(pid, str):
                    await handle_preset(pid)
            elif kind == "mouse_move":
                dx = msg.get("dx")
                dy = msg.get("dy")
                if isinstance(dx, (int, float)) and isinstance(dy, (int, float)):
                    await _dispatch_mouse_move(float(dx), float(dy))
            elif kind == "mouse_click":
                btn = msg.get("button")
                if btn in ("left", "right"):
                    await _dispatch_mouse_click(btn)
            elif kind == "mouse_scroll":
                dx = msg.get("dx") or 0
                dy = msg.get("dy") or 0
                if isinstance(dx, (int, float)) and isinstance(dy, (int, float)):
                    await _dispatch_mouse_scroll(float(dx), float(dy))
            elif kind == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        print(f"[ws] error: {exc}")
    finally:
        state.clients.discard(websocket)
        # Safety: if the phone disconnects mid-hold, release the modifier.
        try:
            right_option_up()
        except Exception:
            pass


def get_lan_ip() -> str:
    """Best-effort LAN IP discovery."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def get_mdns_hostname() -> Optional[str]:
    """Return this Mac's Bonjour / mDNS hostname like "MyMac.local".

    This address is stable — it doesn't change when you switch Wi-Fi
    networks or your Mac gets a new DHCP lease — so it's ideal for
    bookmarking the phone UI as an app.
    """
    try:
        result = subprocess.run(
            ["scutil", "--get", "LocalHostName"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0:
            name = result.stdout.strip()
            if name:
                return f"{name}.local"
    except Exception:
        pass
    return None


def _check_accessibility() -> bool:
    try:
        from ApplicationServices import AXIsProcessTrusted  # type: ignore
        return bool(AXIsProcessTrusted())
    except Exception:
        return False


def _resolve_port() -> int:
    raw = os.environ.get("PORT", "8000").strip()
    try:
        port = int(raw)
    except ValueError:
        sys.stderr.write(f"PORT must be a number, got: {raw!r}\n")
        sys.exit(1)
    if not (1 <= port <= 65535):
        sys.stderr.write(f"PORT out of range: {port}\n")
        sys.exit(1)
    return port


def _port_in_use(port: int) -> bool:
    """Check whether ``port`` is actually bound by a live listener.

    We mirror uvicorn's own socket setup (``SO_REUSEADDR``) so a recently-
    closed socket still in ``TIME_WAIT`` doesn't trigger a false
    "port in use" error.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
        except OSError:
            return True
    return False


def main() -> None:
    import uvicorn

    port = _resolve_port()
    if _port_in_use(port):
        sys.stderr.write(
            f"\nPort {port} is already in use.\n"
            f"  • If Hand Control is already running, just use that instance.\n"
            f"  • Otherwise run on another port:  PORT=8080 ./run.sh\n\n"
        )
        sys.exit(1)

    ip = get_lan_ip()
    hostname = get_mdns_hostname()
    trusted = _check_accessibility()

    # Generate (or reuse) a self-signed TLS cert. We use HTTPS so
    # the phone PWA runs in a "secure context" (required for reliable
    # bookmark + service-worker behavior on iOS) and so installing
    # the cert once on the phone eliminates the per-launch warning.
    try:
        cert = ensure_cert()
        use_https = True
    except Exception as exc:
        print(f"[certs] failed to generate TLS cert: {exc}")
        print("[certs] falling back to HTTP")
        cert = None
        use_https = False

    scheme = "https" if use_https else "http"

    print("\n" + "=" * 64)
    print("  Hand Control running.")
    print()
    if hostname:
        print(f"  Phone URL (stable):  {scheme}://{hostname}:{port}")
        print(f"  Phone URL (by IP):   {scheme}://{ip}:{port}")
        print()
        print(f"  Bookmark the stable URL on your phone — the .local")
        print(f"  hostname won't change when your Wi-Fi does.")
    else:
        print(f"  Phone URL:  {scheme}://{ip}:{port}")
    print("=" * 64)
    if trusted:
        print("  Accessibility: OK (precise Enter timing enabled)")
    else:
        print("  Accessibility: NOT GRANTED")
        print("  → System Settings → Privacy & Security → Accessibility")
        print("    Enable your terminal app, then restart this server.")
        print("  Using hold-duration heuristic for Enter timing until then.")
    if use_https:
        # Compose a pointer to /install using the stable hostname when
        # we have one, or the raw IP otherwise.
        install_host = hostname if hostname else ip
        install_url = f"{scheme}://{install_host}:{port}/install"
        print("=" * 64)
        print("  ONE-TIME SETUP — kill the 'Not Private' warning:")
        print(f"    Visit on your phone:  {install_url}")
        print("    Follow the 4-step install (takes ~45 seconds).")
        print("    After that Safari trusts the site permanently —")
        print("    no more warnings on every launch.")
        print()
        print("  If you'd rather skip it (quick test, etc.):")
        print("    Open the phone URL, tap 'Show Details' →")
        print("    'Visit this website'. You'll re-see this prompt")
        print("    on every future launch until you install the cert.")
    print("=" * 64 + "\n")

    # Scannable QR of the phone URL. Point your phone camera at the
    # terminal and tap the notification that pops up — beats typing a
    # ``.local`` URL into Safari, especially on iOS where there's no
    # history-based autocomplete for ``.local`` hosts.
    phone_url = (
        f"{scheme}://{hostname}:{port}" if hostname else f"{scheme}://{ip}:{port}"
    )
    try:
        import qrcode  # type: ignore

        qr = qrcode.QRCode(
            border=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
        )
        qr.add_data(phone_url)
        qr.make(fit=True)
        print(f"  Scan with your phone camera → {phone_url}\n")
        # ``invert=True`` draws dark modules as whitespace, which looks
        # right on a dark terminal (the default on macOS). The half-
        # block characters keep the QR compact — roughly 20 rows tall
        # for a typical ``.local`` URL.
        qr.print_ascii(invert=True)
        print("")
    except ImportError:
        # qrcode is in requirements.txt but if someone is on an older
        # install we don't want to crash. The URL is still printed in
        # the banner so they can type it manually.
        pass
    except Exception as exc:
        print(f"[qr] couldn't draw QR: {exc}")

    try:
        if use_https and cert is not None:
            uvicorn.run(
                "server.main:app",
                host="0.0.0.0",
                port=port,
                log_level="info",
                ssl_keyfile=str(cert.key_path),
                ssl_certfile=str(cert.cert_path),
            )
        else:
            uvicorn.run(
                "server.main:app",
                host="0.0.0.0",
                port=port,
                log_level="info",
            )
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
