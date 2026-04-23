"""Hand Control peer agent — runs on the Windows PC that sits next
to your Mac.

The Mac is the hub: your phone connects to Mac's server, and the Mac
forwards mouse moves, window focusses, dictation holds, etc. here
over HTTP. We don't talk to the phone directly.

Security model: we assume trusted LAN. All endpoints are open by
default. Set HC_PEER_TOKEN=<some-random-string> and the same env var
on the Mac to require a shared-secret header. Good enough for a
home network; don't expose this to the internet.
"""

from __future__ import annotations

import asyncio
import os
import platform
import socket
import subprocess
import sys
import time
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

# Importing windows_ops on non-Windows immediately blows up (pynput
# pulls in Windows-only stuff). Catch that so `python -m peer.main`
# on Mac at least shows a useful error message instead of a traceback
# 15 frames deep.
if platform.system() != "Windows":
    print(
        "[peer] ERROR: Hand Control peer agent only runs on Windows.\n"
        "       You're on " + platform.system() + ".\n"
        "       On your Mac, run: ./run.sh (from the repo root)."
    )
    sys.exit(1)

from . import windows_ops as ops  # noqa: E402


APP_VERSION = "0.1.0"
DEFAULT_PORT = 8001


# --- Auth helper ------------------------------------------------------------

_EXPECTED_TOKEN = os.environ.get("HC_PEER_TOKEN", "").strip()


def _check_token(x_hc_token: Optional[str]) -> None:
    if not _EXPECTED_TOKEN:
        return
    if x_hc_token != _EXPECTED_TOKEN:
        raise HTTPException(status_code=401, detail="bad peer token")


# --- Shared state -----------------------------------------------------------


class State:
    def __init__(self) -> None:
        self.watcher = ops.KeystrokeWatcherWin()
        self.hold_start_ts: float = 0.0


state = State()


# --- App --------------------------------------------------------------------

app = FastAPI(title="Hand Control Peer", version=APP_VERSION)


@app.on_event("startup")
async def _on_startup() -> None:
    state.watcher.start()
    w, h = ops.primary_screen_size()
    print(
        f"[peer] started — {platform.node()} "
        f"{w}x{h}px — keystroke watcher "
        f"{'active' if state.watcher.active else 'fallback mode'}"
    )


@app.get("/peer/health")
async def health(x_hc_token: Optional[str] = Header(default=None)):
    _check_token(x_hc_token)
    w, h = ops.primary_screen_size()
    return {
        "ok": True,
        "version": APP_VERSION,
        "hostname": platform.node(),
        "screen": {"w": w, "h": h},
        "watcher_active": state.watcher.active,
    }


@app.get("/peer/windows")
async def peer_windows(x_hc_token: Optional[str] = Header(default=None)):
    _check_token(x_hc_token)
    wins = ops.list_cursor_windows()
    return {"windows": [{"title": w.title} for w in wins]}


# --- Mouse endpoints --------------------------------------------------------


@app.post("/peer/mouse_move")
async def mouse_move(
    req: Request,
    x_hc_token: Optional[str] = Header(default=None),
):
    _check_token(x_hc_token)
    body = await req.json()
    dx = body.get("dx")
    dy = body.get("dy")
    if not isinstance(dx, (int, float)) or not isinstance(dy, (int, float)):
        raise HTTPException(400, "dx and dy required")
    # Fire inline — SetInput is microsecond-cheap and we get lots of
    # these per second during a drag.
    try:
        ops.mouse_move_by(float(dx), float(dy))
    except Exception as exc:
        print(f"[peer] mouse_move error: {exc}")
    return {"ok": True}


@app.post("/peer/mouse_click")
async def mouse_click(
    req: Request,
    x_hc_token: Optional[str] = Header(default=None),
):
    _check_token(x_hc_token)
    body = await req.json()
    button = body.get("button", "left")
    if button not in ("left", "right"):
        raise HTTPException(400, "button must be 'left' or 'right'")
    await asyncio.to_thread(ops.mouse_click, button)
    return {"ok": True}


@app.post("/peer/mouse_scroll")
async def mouse_scroll(
    req: Request,
    x_hc_token: Optional[str] = Header(default=None),
):
    _check_token(x_hc_token)
    body = await req.json()
    dx = body.get("dx", 0) or 0
    dy = body.get("dy", 0) or 0
    try:
        ops.mouse_scroll(float(dy), float(dx))
    except Exception as exc:
        print(f"[peer] mouse_scroll error: {exc}")
    return {"ok": True}


@app.post("/peer/warp_cursor")
async def warp_cursor(
    req: Request,
    x_hc_token: Optional[str] = Header(default=None),
):
    _check_token(x_hc_token)
    body = await req.json()
    x = body.get("x")
    y = body.get("y")
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        raise HTTPException(400, "x and y required")
    try:
        ops.warp_cursor(int(x), int(y))
    except Exception as exc:
        print(f"[peer] warp_cursor error: {exc}")
    return {"ok": True}


# --- Dictation + Cursor chat endpoints --------------------------------------


@app.post("/peer/focus_window")
async def focus_window(
    req: Request,
    x_hc_token: Optional[str] = Header(default=None),
):
    _check_token(x_hc_token)
    body = await req.json()
    title = body.get("title")
    if not isinstance(title, str) or not title:
        raise HTTPException(400, "title required")
    ok = await asyncio.to_thread(ops.focus_window, title)
    return {"ok": ok}


@app.post("/peer/hold_start")
async def hold_start(
    req: Request,
    x_hc_token: Optional[str] = Header(default=None),
):
    _check_token(x_hc_token)
    body = await req.json()
    title = body.get("title")
    state.hold_start_ts = time.monotonic()
    if isinstance(title, str) and title:
        await asyncio.to_thread(ops.focus_window, title)
        # Give the OS a beat to raise the window before we press the
        # Wispr hotkey, same as the Mac side.
        await asyncio.sleep(0.08)
    ops.right_alt_down()
    return {"ok": True}


@app.post("/peer/hold_end")
async def hold_end(x_hc_token: Optional[str] = Header(default=None)):
    _check_token(x_hc_token)
    ops.right_alt_up()
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
    return {"ok": True, "auto_submitted": auto_submitted}


@app.post("/peer/submit")
async def submit(x_hc_token: Optional[str] = Header(default=None)):
    """Queue-submit into the currently-focused Cursor chat input."""
    _check_token(x_hc_token)
    await asyncio.to_thread(ops.press_alt_enter)
    return {"ok": True}


@app.post("/peer/delete")
async def delete(x_hc_token: Optional[str] = Header(default=None)):
    _check_token(x_hc_token)
    await asyncio.to_thread(ops.press_ctrl_z)
    return {"ok": True}


@app.post("/peer/type_string")
async def type_string_ep(
    req: Request,
    x_hc_token: Optional[str] = Header(default=None),
):
    _check_token(x_hc_token)
    body = await req.json()
    text = body.get("text")
    if not isinstance(text, str):
        raise HTTPException(400, "text required")
    await asyncio.to_thread(ops.type_string, text)
    return {"ok": True}


@app.post("/peer/press_enter")
async def press_enter_ep(x_hc_token: Optional[str] = Header(default=None)):
    _check_token(x_hc_token)
    await asyncio.to_thread(ops.press_enter)
    return {"ok": True}


@app.post("/peer/focus_chat_input")
async def focus_chat_input_ep(
    x_hc_token: Optional[str] = Header(default=None),
):
    """Ctrl+L — fire Cursor's chat-focus shortcut after a window focus
    so the phone's swipe lands on a ready-to-dictate chat input."""
    _check_token(x_hc_token)
    await asyncio.to_thread(ops.press_ctrl_l)
    return {"ok": True}


# --- Boot -------------------------------------------------------------------


def _lan_ip() -> str:
    """Best-effort LAN IP guess for the status banner."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Doesn't actually connect — just forces the OS to pick the
        # interface it'd use to reach a public IP.
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def _print_banner(port: int) -> None:
    hostname = platform.node()
    ip = _lan_ip()
    token_note = (
        "(shared-secret token required — HC_PEER_TOKEN set)"
        if _EXPECTED_TOKEN
        else "(no auth — trusted LAN only)"
    )
    print("")
    print("=" * 64)
    print("  Hand Control PEER — Windows side is up.")
    print("")
    print(f"  Hostname:  {hostname}:{port}")
    print(f"  LAN IP:    {ip}:{port}")
    print(f"  Auth:      {token_note}")
    print("=" * 64)
    print("")
    print("  On your MAC, paste these two lines into Terminal before")
    print("  running  ./run.sh  (or add them to your shell's rc file):")
    print("")
    print(f"      export HC_PEER_URL=http://{hostname}:{port}")
    print( "      export HC_PC_SIDE=left    # or right / above / below")
    print("")
    print("  (If the hostname doesn't resolve from the Mac, swap it")
    print(f"   for the IP:  http://{ip}:{port})")
    print("")
    print("  Wispr Flow hotkey on this PC must be  Right Alt.")
    print("  Keep this window open — closing it stops the peer.")
    print("=" * 64)
    print("")


def main() -> None:
    import uvicorn

    # Flush output immediately so the banner + startup logs show up
    # right away on slow Windows consoles.
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass

    port = int(os.environ.get("PORT", DEFAULT_PORT))
    _print_banner(port)

    uvicorn.run(
        "peer.main:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
        reload=False,
    )


if __name__ == "__main__":
    main()
