"""List and focus Cursor IDE windows via AppleScript."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass


@dataclass
class CursorWindow:
    title: str   # normalized title, without the "●" unsaved-indicator
    project: str


def _osascript(script: str) -> str:
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=3,
    )
    if result.returncode != 0:
        raise RuntimeError(f"osascript failed: {result.stderr.strip()}")
    return result.stdout.strip()


_LIST_SCRIPT = '''
tell application "System Events"
    if not (exists process "Cursor") then return ""
    tell process "Cursor"
        set titles to {}
        repeat with w in windows
            set end of titles to name of w
        end repeat
        set AppleScript's text item delimiters to "\\n"
        return titles as string
    end tell
end tell
'''


def _normalize(title: str) -> str:
    """Strip the '●' unsaved-file indicator and surrounding whitespace.

    Cursor toggles this prefix as files get modified/saved, so using raw
    titles as selection identity makes the selection jump around.
    """
    if not title:
        return ""
    # Strip any leading combination of bullet/dots and spaces
    return re.sub(r"^[●•·\s]+", "", title).strip()


def _extract_project(title: str) -> str:
    # Cursor window titles usually look like:
    #   "filename.ext — project-name"
    #   "project-name"
    # Project is after the last em-dash / en-dash / hyphen-with-spaces.
    if not title:
        return ""
    parts = re.split(r"\s[—–-]\s", title)
    project = parts[-1].strip() if parts else title.strip()
    return project or title


def list_windows() -> list[CursorWindow]:
    try:
        raw = _osascript(_LIST_SCRIPT)
    except Exception:
        return []
    if not raw:
        return []
    windows: list[CursorWindow] = []
    seen = set()
    for line in raw.splitlines():
        title = _normalize(line.strip())
        if not title or title in seen:
            continue
        seen.add(title)
        windows.append(CursorWindow(title=title, project=_extract_project(title)))
    return windows


def focus_window(title: str) -> bool:
    """Raise a specific Cursor window to the front.

    Accepts a normalized title (no '●' prefix) and finds the matching
    window regardless of whether its actual title currently has the
    dirty-file indicator or not.
    """
    safe = title.replace('"', '\\"')
    script = f'''
    tell application "System Events"
        if not (exists process "Cursor") then return "miss"
        tell process "Cursor"
            set frontmost to true
            set target to "{safe}"
            repeat with w in every window
                set wName to name of w
                -- Strip any leading dirty-dot indicator before comparing
                set normalized to wName
                repeat while normalized starts with "●" or normalized starts with "•" or normalized starts with " "
                    set normalized to text 2 thru -1 of normalized
                end repeat
                if normalized is equal to target then
                    perform action "AXRaise" of w
                    return "ok"
                end if
            end repeat
            return "miss"
        end tell
    end tell
    '''
    try:
        out = _osascript(script)
        return out == "ok"
    except Exception:
        return False
