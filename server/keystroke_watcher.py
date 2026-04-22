"""Global keystroke watcher using CGEventTap.

Tracks the timestamp of the most recent keydown event coming from *any*
source. Used to detect when Wispr Flow has finished typing its transcription
so we know when to fire Enter.

Requires Accessibility permission granted to the process running this
(Terminal.app, iTerm, Cursor, Python binary — whatever you launch from).
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from Quartz import (
    CFMachPortCreateRunLoopSource,
    CFRunLoopAddSource,
    CFRunLoopGetCurrent,
    CFRunLoopRun,
    CGEventMaskBit,
    CGEventTapCreate,
    CGEventTapEnable,
    kCFRunLoopCommonModes,
    kCGEventKeyDown,
    kCGEventTapOptionListenOnly,
    kCGHeadInsertEventTap,
    kCGSessionEventTap,
)


class KeystrokeWatcher:
    """Maintain a monotonically updated `last_keydown_ts` timestamp."""

    def __init__(self) -> None:
        self._last_ts: float = 0.0
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._tap = None
        self._started = False
        self.active: bool = False  # True once CGEventTap is running

    @property
    def last_keydown_ts(self) -> float:
        with self._lock:
            return self._last_ts

    def _callback(self, proxy, type_, event, refcon):
        with self._lock:
            self._last_ts = time.monotonic()
        return event

    def _run(self) -> None:
        mask = CGEventMaskBit(kCGEventKeyDown)
        self._tap = CGEventTapCreate(
            kCGSessionEventTap,
            kCGHeadInsertEventTap,
            kCGEventTapOptionListenOnly,
            mask,
            self._callback,
            None,
        )
        if not self._tap:
            # Most likely: Accessibility permission not granted.
            print(
                "[keystroke_watcher] Failed to create event tap. "
                "Enter-timing will use a heuristic fallback based on hold "
                "duration. Grant Accessibility permission to your terminal "
                "in System Settings → Privacy & Security → Accessibility, "
                "then restart the server for precise Enter timing."
            )
            return
        source = CFMachPortCreateRunLoopSource(None, self._tap, 0)
        CFRunLoopAddSource(CFRunLoopGetCurrent(), source, kCFRunLoopCommonModes)
        CGEventTapEnable(self._tap, True)
        self.active = True
        CFRunLoopRun()

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def wait_for_typing_to_settle(
        self,
        release_ts: float,
        hold_duration_s: float,
        idle_ms: int = 400,
        first_key_timeout_s: float = 2.5,
        max_wait_s: float = 10.0,
        poll_ms: int = 50,
    ) -> None:
        """Block until Wispr Flow has finished typing, then return.

        Two-phase behavior when the event tap is active:
            1. Wait up to `first_key_timeout_s` for the first keystroke
               after release (Wispr starting to type).
            2. Once it starts, wait until keystrokes go quiet for `idle_ms`.

        If the event tap isn't active (Accessibility not granted) or no
        keystroke ever arrives, fall back to a heuristic delay based on
        how long the hold lasted. This keeps things usable without
        Accessibility permission, at the cost of precision.
        """
        poll_s = poll_ms / 1000.0
        idle_s = idle_ms / 1000.0

        def _heuristic_fallback() -> None:
            # Wispr's transcription latency scales roughly with audio length.
            # Empirically: ~0.4s overhead + ~30% of hold duration, capped.
            extra = 0.4 + min(hold_duration_s * 0.3, 3.0)
            time.sleep(extra)

        if not self.active:
            _heuristic_fallback()
            return

        # Phase 1: wait for Wispr's first keystroke.
        # Allow more time for long dictations (server-side transcription
        # can take a while to start).
        first_deadline = release_ts + max(first_key_timeout_s, hold_duration_s * 0.6)
        while True:
            now = time.monotonic()
            if self.last_keydown_ts > release_ts:
                break
            if now > first_deadline:
                # No typing detected — either Wispr failed, or the tap
                # isn't actually seeing keystrokes. Use heuristic.
                _heuristic_fallback()
                return
            time.sleep(poll_s)

        # Phase 2: wait for typing to go quiet.
        hard_deadline = release_ts + max_wait_s
        while True:
            now = time.monotonic()
            if now > hard_deadline:
                return
            if now - self.last_keydown_ts >= idle_s:
                return
            time.sleep(poll_s)
