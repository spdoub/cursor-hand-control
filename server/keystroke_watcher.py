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
                "Grant Accessibility permission to your terminal / Python "
                "binary in System Settings → Privacy & Security → Accessibility."
            )
            return
        source = CFMachPortCreateRunLoopSource(None, self._tap, 0)
        CFRunLoopAddSource(CFRunLoopGetCurrent(), source, kCFRunLoopCommonModes)
        CGEventTapEnable(self._tap, True)
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
        idle_ms: int = 400,
        max_wait_s: float = 8.0,
        poll_ms: int = 50,
    ) -> None:
        """Block until keystrokes have been quiet for `idle_ms` past
        `release_ts`, or `max_wait_s` elapses.

        `release_ts` is the time.monotonic() value taken right after we
        released Right Option. Any keystroke after that is assumed to be
        Wispr Flow typing out the transcription.
        """
        deadline = release_ts + max_wait_s
        idle_s = idle_ms / 1000.0
        poll_s = poll_ms / 1000.0

        while True:
            now = time.monotonic()
            if now > deadline:
                return
            last = self.last_keydown_ts
            # Only count keydowns that happened after we released.
            effective_last = max(last, release_ts)
            if now - effective_last >= idle_s:
                return
            time.sleep(poll_s)
