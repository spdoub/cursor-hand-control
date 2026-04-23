#!/bin/bash
#
# Hand Control — one-command setup for macOS.
#
# Run this once after cloning:
#
#     ./install.sh
#
# It walks you through the whole setup, step by step, and is fully
# idempotent — safe to rerun whenever.
#
# What it does:
#   1. Checks macOS + Python 3.10+ (offers to brew-install Python if
#      missing and Homebrew is available).
#   2. Creates .venv/ and installs Python dependencies.
#   3. Builds a double-clickable Hand Control.app in ~/Applications/.
#   4. Pre-generates the self-signed HTTPS cert so the cert-trust
#      install flow works on the very first phone visit.
#   5. Checks that Wispr Flow is installed; links out if not.
#   6. Opens the Accessibility privacy pane so you can grant that
#      permission ahead of first launch.
#   7. Prints the exact URLs you'll need on your phone, + a link
#      to the Windows-peer instructions if you also want to control
#      a PC from the same remote.

set -euo pipefail

cd "$(dirname "$0")"
REPO_DIR="$(pwd)"

# --- Pretty printing helpers ----------------------------------------------

bold()   { printf '\033[1m%s\033[0m' "$1"; }
dim()    { printf '\033[2m%s\033[0m' "$1"; }
say()    { printf '  %s\n' "$*"; }
ok()     { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn()   { printf '  \033[33m!\033[0m %s\n' "$*"; }
fail()   { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; }
step()   { printf '\n\033[1;36m%s\033[0m\n' "$1"; printf '%*s\n' "${#1}" '' | tr ' ' '='; }
big()    { printf '\n\033[1m%s\033[0m\n\n' "$1"; }

# Interactive yes/no. Returns 0 on yes, 1 on no. Default is no
# unless a second arg ``y`` is passed.
ask_yn() {
  local prompt="$1" default="${2:-n}" ans
  if [ "$default" = "y" ]; then prompt="$prompt [Y/n] "
  else prompt="$prompt [y/N] "; fi
  # If running non-interactively (e.g. piped from curl), honor the
  # default rather than hanging forever.
  if [ ! -t 0 ]; then
    [ "$default" = "y" ] && return 0 || return 1
  fi
  read -r -p "  $prompt" ans || ans=""
  ans="${ans:-$default}"
  case "$ans" in
    y|Y|yes|YES) return 0 ;;
    *)           return 1 ;;
  esac
}

# --- 1. Platform check -----------------------------------------------------

step "1/6  Checking your Mac"

if [ "$(uname)" != "Darwin" ]; then
  fail "Hand Control's server runs only on macOS (detected: $(uname))."
  say  "Wrong machine? On Windows you want peer\\install.bat instead,"
  say  "which sets up the PC-side peer agent."
  exit 1
fi
ok "macOS $(sw_vers -productVersion 2>/dev/null || echo detected)"

# --- 2. Python 3.10+ -------------------------------------------------------

step "2/6  Python 3.10+"

install_python_via_brew() {
  if ! command -v brew >/dev/null 2>&1; then
    warn "Homebrew not found — skipping auto-install."
    return 1
  fi
  say "Running: brew install python@3.12"
  brew install python@3.12 || return 1
  # Ensure it's on PATH for the rest of this script run.
  if [ -x /opt/homebrew/bin/python3 ]; then
    export PATH="/opt/homebrew/bin:$PATH"
  elif [ -x /usr/local/bin/python3 ]; then
    export PATH="/usr/local/bin:$PATH"
  fi
  return 0
}

if ! command -v python3 >/dev/null 2>&1; then
  fail "python3 not found."
  if ask_yn "Install Python 3.12 via Homebrew right now?" y; then
    if install_python_via_brew && command -v python3 >/dev/null 2>&1; then
      ok "Python installed via Homebrew"
    else
      fail "Couldn't install Python automatically."
      say  "Install manually: https://www.python.org/downloads/ (pick 3.12)."
      say  "Or install Homebrew first: https://brew.sh then rerun this script."
      exit 1
    fi
  else
    say  "No problem — install Python 3.10+ from:"
    say  "  • https://www.python.org/downloads/  (easiest)"
    say  "  • or  brew install python@3.12"
    say  "…then rerun ./install.sh."
    exit 1
  fi
fi

PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}.{sys.version_info[2]}")')
PY_OK=$(python3 -c 'import sys; print("1" if sys.version_info >= (3, 10) else "0")')
if [ "$PY_OK" != "1" ]; then
  fail "Python 3.10+ required. Found: $PY_VER"
  say  "Install a newer one with:  brew install python@3.12"
  exit 1
fi
ok "Python $PY_VER"

# --- 3. Virtualenv + Python dependencies -----------------------------------

step "3/6  Python dependencies"

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
  ok "created .venv"
else
  ok ".venv already exists"
fi

# shellcheck disable=SC1091
source .venv/bin/activate

python3 -m pip install --upgrade pip --quiet || true
if python3 -m pip install -r requirements.txt --quiet; then
  ok "installed Python packages"
else
  fail "pip install failed (scroll up for the real error)."
  say  "Common fixes:"
  say  "  • no internet access? try again with Wi-Fi on"
  say  "  • SSL error? run:  /Applications/Python*/Install\\ Certificates.command"
  exit 1
fi

# --- 4. Pre-generate the self-signed HTTPS cert ----------------------------
#
# Not strictly required (the server does this on first launch too),
# but doing it here means the ``/install`` cert-trust flow works the
# moment the user opens the URL, instead of waiting for the first
# launch to populate ``./certs/``.

step "4/6  HTTPS certificate"

if python3 -c "from server.certs import ensure_cert; ensure_cert()" 2>/dev/null; then
  ok "self-signed cert ready at ./certs/server.crt"
else
  warn "couldn't pre-generate cert — no big deal, it'll be made on first launch."
fi

# --- 5. App bundle + one-click launch shortcuts ---------------------------

step "5/6  Hand Control.app  +  launch shortcuts"

chmod +x scripts/build-app.sh
./scripts/build-app.sh

APP_PATH="$HOME/Applications/Hand Control.app"

# 5a. Desktop alias so double-clicking from the desktop just works.
if [ -d "$APP_PATH" ]; then
  osascript >/dev/null 2>&1 <<OSA || true
tell application "Finder"
    if exists (alias file "Hand Control" of desktop) then
        delete (alias file "Hand Control" of desktop)
    end if
    make new alias file at desktop to (POSIX file "$APP_PATH")
    set name of result to "Hand Control"
end tell
OSA
  if [ -e "$HOME/Desktop/Hand Control" ]; then
    ok "Desktop shortcut: ~/Desktop/Hand Control"
  fi

  # 5b. Pin to the Dock (no-op if already there).
  #
  # defaults serializes the app path URL-encoded ("Hand%20Control.app")
  # and the bundle-identifier uncoded ("com.handcontrol.launcher"), so
  # we match on the bundle ID — the one thing that's both unique and
  # immune to path formatting quirks.
  if defaults read com.apple.dock persistent-apps 2>/dev/null \
       | grep -q 'com.handcontrol.launcher'; then
    ok "Dock:  already pinned"
  else
    ENCODED_PATH="${APP_PATH// /%20}"
    DOCK_ENTRY="<dict><key>tile-data</key><dict><key>file-data</key><dict><key>_CFURLString</key><string>file://${ENCODED_PATH}/</string><key>_CFURLStringType</key><integer>15</integer></dict></dict></dict>"
    if defaults write com.apple.dock persistent-apps -array-add "$DOCK_ENTRY" 2>/dev/null; then
      killall Dock 2>/dev/null || true
      ok "Dock:  pinned Hand Control"
    else
      warn "couldn't add to Dock automatically (not fatal — drag the icon from ~/Applications)."
    fi
  fi
fi

# --- 6. Final setup: Wispr Flow + macOS permissions ------------------------

step "6/6  Wispr Flow + macOS permissions"

# Wispr Flow detection. It's a paid product, so we can't install it
# for you — just nudge and link.
WISPR_INSTALLED=0
if [ -d "/Applications/Wispr Flow.app" ] || [ -d "$HOME/Applications/Wispr Flow.app" ]; then
  WISPR_INSTALLED=1
  ok "Wispr Flow detected"
else
  warn "Wispr Flow not found in /Applications."
  say  "Hand Control uses Wispr Flow to do the actual voice-to-text:"
  say  "  • Download:  https://wisprflow.ai"
  say  "  • In Wispr settings, set the dictation hotkey to  Right Option."
  if ask_yn "Open the Wispr Flow site now?" n; then
    open "https://wisprflow.ai" || true
  fi
fi

# Accessibility permission pane.
say  ""
say  "Opening System Settings → Privacy → Accessibility."
say  "Check the box next to your terminal app (Terminal / iTerm /"
say  "whatever you're running this from) so Hand Control can"
say  "simulate key presses on your behalf. When Hand Control also"
say  "asks for Automation later, click Allow."
open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility" 2>/dev/null || true

# --- Farewell --------------------------------------------------------------

# Compute the exact URLs we're about to tell the user to open.
HOSTNAME_LOCAL=""
if command -v scutil >/dev/null 2>&1; then
  LH=$(scutil --get LocalHostName 2>/dev/null || true)
  [ -n "$LH" ] && HOSTNAME_LOCAL="${LH}.local"
fi
PHONE_URL="https://${HOSTNAME_LOCAL:-your-mac.local}:8000"
INSTALL_URL="${PHONE_URL}/install"

big "All set. Here's what to do next:"

# Use printf so the ANSI escape codes actually render; a plain
# heredoc would print the literal \033 bytes.
B="$(printf '\033[1m')"       # bold on
R="$(printf '\033[0m')"       # reset
Y="$(printf '\033[33m')"      # yellow

printf '%s\n' "\
  1.  ${B}Launch Hand Control${R}
      Click the Hand Control icon in your Dock,
      or double-click the 'Hand Control' shortcut on your Desktop.
      (Cmd+Space → \"Hand Control\" → Return also works.)
      A Terminal window opens with a scannable QR code.

  2.  ${B}Point your phone's camera at the QR code${R}
      Tap the notification → Safari opens the phone remote.
      (Bookmark/\"Add to Home Screen\" it for an app icon.)

  3.  ${B}Kill the \"Not Private\" warning (one-time, ~45 sec)${R}
      On your phone, open:
          ${INSTALL_URL}
      Follow the 4 steps. Afterwards, Safari trusts the site
      permanently — no more warning on every launch.

  4.  ${B}Start talking${R}
      Swipe between Cursor windows, press-and-hold to dictate,
      tap Submit to queue the message in Cursor.
"

if [ "$WISPR_INSTALLED" -eq 0 ]; then
  printf '  %sReminder:%s install Wispr Flow (https://wisprflow.ai) and\n' "$Y" "$R"
  printf '  set its dictation hotkey to %sRight Option%s before you test.\n\n' "$B" "$R"
fi

printf '%s\n' "\
  Want to also control a Windows PC from the same phone remote?
  See the \"Two-machine mode (Mac + PC)\" section of README.md.
  On the Windows side, run  peer\\install.bat  to do the one-command
  setup there too.
"
