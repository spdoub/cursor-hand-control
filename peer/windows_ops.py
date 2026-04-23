"""Windows-specific operations used by the Hand Control peer agent.

Everything that touches the Win32 API lives here so the rest of the
peer agent is plain Python. All functions assume they run in the user
session (not a Windows service) because they rely on the foreground
window and the global input queue.

If any import fails, this module still imports cleanly — individual
functions will raise informative errors. That lets the peer's HTTP
server boot and report itself as unhealthy instead of crashing.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading
import time
from dataclasses import dataclass
from typing import List, Optional

# pynput for keyboard + mouse simulation. It wraps SendInput internally
# but gives us a much nicer API than raw ctypes.
try:
    from pynput import keyboard as _kb
    from pynput import mouse as _mouse
except ImportError as exc:  # pragma: no cover
    raise RuntimeError(
        "pynput is required on Windows. Install with "
        "'pip install -r peer/requirements.txt'."
    ) from exc

# pywin32 for window enumeration and foregrounding.
try:
    import win32con
    import win32gui
    import win32process
except ImportError as exc:  # pragma: no cover
    raise RuntimeError(
        "pywin32 is required on Windows. Install with "
        "'pip install -r peer/requirements.txt'."
    ) from exc


# --- Low-level handles used by multiple functions ---------------------------

_user32 = ctypes.windll.user32
_mouse_ctrl = _mouse.Controller()
_kb_ctrl = _kb.Controller()


# --- Screen geometry --------------------------------------------------------


def primary_screen_size() -> tuple[int, int]:
    """Return the (width, height) of the primary Windows display.

    We deliberately ask for the PRIMARY monitor only. Supporting
    full multi-monitor Windows layouts from a single edge-crossing
    model is more complex than we need for v1; users who want it can
    extend this later.
    """
    # SM_CXSCREEN = 0, SM_CYSCREEN = 1
    w = int(_user32.GetSystemMetrics(0))
    h = int(_user32.GetSystemMetrics(1))
    return (w, h)


# --- Mouse ------------------------------------------------------------------


def mouse_move_by(dx: float, dy: float) -> None:
    """Move the cursor relative to its current position.

    pynput's .move(dx, dy) uses SendInput with MOUSEEVENTF_MOVE, which
    respects the user's mouse acceleration curve. That matches the
    Mac-side behavior and feels correct during trackpad use.
    """
    # pynput expects ints. Sub-pixel deltas from the phone get
    # truncated, which is what we want — sending 0s is cheap.
    _mouse_ctrl.move(int(dx), int(dy))


def mouse_click(button: str = "left") -> None:
    btn = _mouse.Button.right if button == "right" else _mouse.Button.left
    # One explicit press/release pair with a small gap. Same reasoning
    # as the Mac side: bursty events in the same tick can get
    # coalesced by Electron apps like Cursor and lost.
    _mouse_ctrl.press(btn)
    time.sleep(0.035)
    _mouse_ctrl.release(btn)


def mouse_scroll(dy: float, dx: float = 0.0) -> None:
    """Scroll by ``(dx, dy)``.

    pynput uses "clicks" as the scroll unit (one notch of a mouse
    wheel ≈ 120 WHEEL_DELTA units under the hood). Translating phone
    pixel-deltas to wheel clicks: divide by 40 as a reasonable
    starting feel, matching macOS pixel-unit scrolling density.
    """
    cx = dx / 40.0
    cy = -dy / 40.0  # pynput scroll y is positive=up, we want natural
    if cx == 0 and cy == 0:
        return
    _mouse_ctrl.scroll(cx, cy)


def warp_cursor(x: int, y: int) -> None:
    """Move the cursor to an absolute screen coordinate."""
    _user32.SetCursorPos(int(x), int(y))


def cursor_position() -> tuple[int, int]:
    pt = wt.POINT()
    _user32.GetCursorPos(ctypes.byref(pt))
    return (int(pt.x), int(pt.y))


# --- Keyboard ---------------------------------------------------------------
#
# Wispr Flow on Windows can be bound to Right Alt as its "hold to
# talk" key. That's the closest equivalent to Right Option on Mac
# (it's physically in the same spot and, like Right Option, it
# doesn't have a common single-key shortcut conflict).


def right_alt_down() -> None:
    _kb_ctrl.press(_kb.Key.alt_r)


def right_alt_up() -> None:
    _kb_ctrl.release(_kb.Key.alt_r)


def press_enter() -> None:
    _kb_ctrl.press(_kb.Key.enter)
    _kb_ctrl.release(_kb.Key.enter)


def press_alt_enter() -> None:
    """Windows equivalent of Option+Enter — Cursor's "queue message"
    shortcut on Windows is Alt+Enter."""
    _kb_ctrl.press(_kb.Key.alt)
    _kb_ctrl.press(_kb.Key.enter)
    _kb_ctrl.release(_kb.Key.enter)
    _kb_ctrl.release(_kb.Key.alt)


def press_ctrl_z() -> None:
    """Windows equivalent of Cmd+Z."""
    _kb_ctrl.press(_kb.Key.ctrl)
    _kb_ctrl.press("z")
    _kb_ctrl.release("z")
    _kb_ctrl.release(_kb.Key.ctrl)


def press_ctrl_l() -> None:
    """Windows equivalent of Cmd+L — Cursor's "open AI chat / focus
    chat input" shortcut. Fired right after a window-focus so the
    phone swipe lands on a ready-to-dictate chat input.
    """
    _kb_ctrl.press(_kb.Key.ctrl)
    _kb_ctrl.press("l")
    _kb_ctrl.release("l")
    _kb_ctrl.release(_kb.Key.ctrl)


def type_string(text: str) -> None:
    """Type ``text`` into the currently-focused UI element."""
    # pynput's .type() already paces characters sensibly; no extra
    # delay needed on Windows for Electron apps in my testing.
    _kb_ctrl.type(text)


# --- Cursor window enumeration ----------------------------------------------


@dataclass
class WinCursorWindow:
    title: str
    hwnd: int


def _get_process_name(hwnd: int) -> str:
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
    except Exception:
        return ""
    if not pid:
        return ""

    # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000 (works even for
    # Electron child processes under restricted tokens)
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h_proc = ctypes.windll.kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, pid
    )
    if not h_proc:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(len(buf))
        # QueryFullProcessImageNameW is the modern replacement for
        # GetModuleFileNameEx, and doesn't require psapi.dll.
        ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(
            h_proc, 0, buf, ctypes.byref(size)
        )
        if not ok:
            return ""
        return buf.value
    finally:
        ctypes.windll.kernel32.CloseHandle(h_proc)


def list_cursor_windows() -> List[WinCursorWindow]:
    """Enumerate top-level windows belonging to Cursor.exe.

    We match on the process executable name rather than the window
    title so we don't confuse "something — Cursor" windows owned by
    other apps. Dirty indicators (a leading "●") are stripped so the
    title stays stable across saves, same as the Mac side.
    """
    results: List[WinCursorWindow] = []

    def _cb(hwnd: int, _lparam) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return True
        exe = _get_process_name(hwnd).lower()
        if not exe.endswith("\\cursor.exe"):
            return True
        # Strip Electron's "● " dirty-indicator prefix so the title
        # doesn't flicker in the deck every time you save a file.
        clean = title.lstrip("● ").strip()
        if clean:
            results.append(WinCursorWindow(title=clean, hwnd=hwnd))
        return True

    win32gui.EnumWindows(_cb, None)

    # Sort by title for stable ordering, matching the Mac side.
    results.sort(key=lambda w: w.title.lower())
    return results


def focus_window(title: str) -> bool:
    """Bring the first visible Cursor window matching ``title`` to
    the foreground. Returns True if we found and raised one."""
    needle = title.lstrip("● ").strip().lower()
    for win in list_cursor_windows():
        if win.title.lower() == needle:
            try:
                # Restore if minimized.
                if win32gui.IsIconic(win.hwnd):
                    win32gui.ShowWindow(win.hwnd, win32con.SW_RESTORE)
                # SetForegroundWindow can silently fail if no input
                # events have been sent recently. AttachThreadInput
                # is the classic workaround, but in our case we've
                # just been processing pointer events from the phone,
                # so the foreground lock is usually released.
                win32gui.SetForegroundWindow(win.hwnd)
                return True
            except Exception:
                return False
    return False


# --- Global keystroke watcher (for auto-submit detection) -------------------
#
# pynput's keyboard.Listener installs a low-level keyboard hook, same
# mechanism as the Mac's CGEventTap. We use it for exactly the same
# two things: (1) a last-keydown timestamp (so we can detect when
# Wispr has stopped typing), and (2) a last-Enter timestamp (so we
# know when Wispr auto-pressed Enter via its "press enter" command).


class KeystrokeWatcherWin:
    def __init__(self) -> None:
        self._last_ts: float = 0.0
        self._last_return_ts: float = 0.0
        self._lock = threading.Lock()
        self._listener: Optional[_kb.Listener] = None
        self.active: bool = False

    def start(self) -> None:
        if self._listener is not None:
            return

        def _on_press(key):
            now = time.monotonic()
            with self._lock:
                self._last_ts = now
                if key == _kb.Key.enter:
                    self._last_return_ts = now

        try:
            self._listener = _kb.Listener(on_press=_on_press)
            self._listener.start()
            self.active = True
        except Exception as exc:
            # Running inside a restricted session or antivirus quirks
            # can block the hook. Fall back to a timer-only heuristic
            # — the server still works, just with a fixed wait.
            print(f"[keystroke_watcher_win] hook failed: {exc}")
            self._listener = None
            self.active = False

    @property
    def last_keydown_ts(self) -> float:
        with self._lock:
            return self._last_ts

    def saw_return_since(self, ts: float) -> bool:
        with self._lock:
            return self._last_return_ts > ts

    def wait_for_typing_to_settle(
        self,
        release_ts: float,
        hold_duration: float,
        idle_ms: int = 450,
        max_wait_s: float = 6.0,
    ) -> None:
        """Block until we believe Wispr has finished typing.

        Mirrors the Mac side's logic: if the hook is active we wait
        until we've seen `idle_ms` go by with no keydown. Otherwise
        we fall back to a duration-proportional fixed wait.
        """
        if not self.active:
            # Heuristic: Wispr's typing takes roughly 1.2× the hold
            # duration on Windows in my testing. Cap the wait so a
            # long rambling dictation doesn't block the UI forever.
            wait = min(max(hold_duration * 1.2, 0.8), max_wait_s)
            time.sleep(wait)
            return

        deadline = release_ts + max_wait_s
        idle_s = idle_ms / 1000.0
        while True:
            now = time.monotonic()
            if now >= deadline:
                return
            with self._lock:
                last = self._last_ts
            quiet_for = now - max(last, release_ts)
            if quiet_for >= idle_s:
                return
            # Sleep until either idle_ms has elapsed since the last
            # keydown or until the next deadline, whichever's first.
            sleep_s = min(idle_s - quiet_for, deadline - now, 0.05)
            if sleep_s > 0:
                time.sleep(sleep_s)
