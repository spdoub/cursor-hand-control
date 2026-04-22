"""List and focus Cursor IDE windows via AppleScript."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass


@dataclass
class CursorWindow:
    title: str
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


def _extract_project(title: str) -> str:
    # Cursor window titles usually look like:
    #   "filename.ext — project-name"
    #   "project-name"
    #   "● filename — project-name"
    # The project name is after the last em-dash or hyphen-with-spaces.
    if not title:
        return ""
    parts = re.split(r"\s[—–-]\s", title)
    project = parts[-1].strip() if parts else title.strip()
    # Strip any leading dirty-dot indicator
    project = project.lstrip("● ").strip()
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
        title = line.strip()
        if not title or title in seen:
            continue
        seen.add(title)
        windows.append(CursorWindow(title=title, project=_extract_project(title)))
    return windows


def focus_window(title: str) -> bool:
    """Raise a specific Cursor window to the front by its title."""
    # Escape embedded double quotes
    safe = title.replace('"', '\\"')
    script = f'''
    tell application "System Events"
        tell process "Cursor"
            set frontmost to true
            try
                perform action "AXRaise" of (first window whose name is "{safe}")
                return "ok"
            on error
                return "miss"
            end try
        end tell
    end tell
    '''
    try:
        out = _osascript(script)
        return out == "ok"
    except Exception:
        return False
