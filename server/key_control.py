"""Simulate keyboard events via Quartz (CoreGraphics).

We simulate:
- Right Option (keycode 61) press + release — Wispr Flow's activation hotkey.
- Return / Enter (keycode 36) — to submit the transcribed line.
"""

from __future__ import annotations

from Quartz import (
    CGEventCreateKeyboardEvent,
    CGEventPost,
    CGEventSetFlags,
    kCGHIDEventTap,
    kCGEventFlagMaskAlternate,
)

KEYCODE_RIGHT_OPTION = 61
KEYCODE_RETURN = 36


def _post(keycode: int, is_down: bool, flags: int = 0) -> None:
    event = CGEventCreateKeyboardEvent(None, keycode, is_down)
    if flags:
        CGEventSetFlags(event, flags)
    CGEventPost(kCGHIDEventTap, event)


def right_option_down() -> None:
    # We set the Option flag mask on the key-down so downstream listeners
    # (like Wispr Flow) see the modifier state correctly.
    _post(KEYCODE_RIGHT_OPTION, True, flags=kCGEventFlagMaskAlternate)


def right_option_up() -> None:
    _post(KEYCODE_RIGHT_OPTION, False, flags=0)


def press_enter() -> None:
    _post(KEYCODE_RETURN, True)
    _post(KEYCODE_RETURN, False)
