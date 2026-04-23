"""Simulate keyboard events via Quartz (CoreGraphics)."""

from __future__ import annotations

import time

from Quartz import (
    CGEventCreateKeyboardEvent,
    CGEventKeyboardSetUnicodeString,
    CGEventPost,
    CGEventSetFlags,
    kCGEventFlagMaskAlternate,
    kCGEventFlagMaskCommand,
    kCGHIDEventTap,
)

KEYCODE_RIGHT_OPTION = 61
KEYCODE_RETURN = 36
KEYCODE_Z = 6  # kVK_ANSI_Z
KEYCODE_L = 37  # kVK_ANSI_L

# Tiny gap between synthesized keystrokes when typing a string. Most apps
# handle bursts fine, but Electron chat inputs (Cursor, VSCode) can drop
# characters if we fire them all at once in the same tick.
_TYPE_DELAY_S = 0.004


def _post(keycode: int, is_down: bool, flags: int = 0) -> None:
    event = CGEventCreateKeyboardEvent(None, keycode, is_down)
    if flags:
        CGEventSetFlags(event, flags)
    CGEventPost(kCGHIDEventTap, event)


def right_option_down() -> None:
    _post(KEYCODE_RIGHT_OPTION, True, flags=kCGEventFlagMaskAlternate)


def right_option_up() -> None:
    _post(KEYCODE_RIGHT_OPTION, False, flags=0)


def press_enter() -> None:
    _post(KEYCODE_RETURN, True)
    _post(KEYCODE_RETURN, False)


def press_option_enter() -> None:
    """Simulate Option+Enter — Cursor's "queue message" shortcut.

    When the agent is running, this appends the message to the queue to
    be processed after the current run finishes, instead of interrupting.
    When the agent is idle, it just submits normally.
    """
    _post(KEYCODE_RETURN, True, flags=kCGEventFlagMaskAlternate)
    _post(KEYCODE_RETURN, False, flags=kCGEventFlagMaskAlternate)


def press_cmd_z() -> None:
    """Simulate Cmd+Z to undo the last Wispr Flow insertion."""
    _post(KEYCODE_Z, True, flags=kCGEventFlagMaskCommand)
    _post(KEYCODE_Z, False, flags=kCGEventFlagMaskCommand)


def press_cmd_l() -> None:
    """Simulate Cmd+L — Cursor's "open AI chat / focus chat input"
    shortcut on macOS.

    Fired right after a window focus so the phone's swipe-to-new-card
    gesture lands the user on a ready-to-dictate chat input, not on
    whatever the window last had selected (often the code editor).
    """
    _post(KEYCODE_L, True, flags=kCGEventFlagMaskCommand)
    _post(KEYCODE_L, False, flags=kCGEventFlagMaskCommand)


def type_string(text: str) -> None:
    """Type ``text`` into the currently-focused UI element.

    Uses ``CGEventKeyboardSetUnicodeString`` so any Unicode character
    can be emitted without caring about physical keycodes or keyboard
    layout. A tiny delay between characters prevents Electron-based
    chat inputs (Cursor) from dropping fast bursts.
    """
    for ch in text:
        down = CGEventCreateKeyboardEvent(None, 0, True)
        CGEventKeyboardSetUnicodeString(down, 1, ch)
        CGEventPost(kCGHIDEventTap, down)

        up = CGEventCreateKeyboardEvent(None, 0, False)
        CGEventKeyboardSetUnicodeString(up, 1, ch)
        CGEventPost(kCGHIDEventTap, up)

        if _TYPE_DELAY_S > 0:
            time.sleep(_TYPE_DELAY_S)
