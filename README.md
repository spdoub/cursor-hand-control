# Hand Control

**Your phone, as a remote for dictating into multiple Cursor windows.**

Turn your phone into a touch-screen remote that lets you:

1. See every open Cursor window as a tappable box.
2. Pick which window you want to talk to.
3. Press-and-hold a big button on your phone to dictate into it.
4. Let go — the system automatically waits for Wispr Flow to finish
   transcribing, then presses Enter for you.

Your AirPods (connected to your Mac) are still the mic. The phone is just a
remote control — **no audio ever goes over the network**.

---

## Why?

If you dictate code with Wispr Flow and juggle multiple Cursor windows, you
know the dance: click into a window, hold `Fn`, talk, release, press Enter,
repeat for the next window. Hand Control collapses that into a single
touch-and-hold on your phone while you keep your eyes on your Mac screen.

---

## How it works

```
┌────────────────────┐     WebSocket      ┌────────────────────────┐
│   Phone (browser)  │  ◄──────────────►  │  Mac (Python server)   │
│   landscape remote │                    │                        │
└────────────────────┘                    │  ┌──────────────────┐  │
                                          │  │   AppleScript    │  │
                                          │  │ (list + focus    │  │
                                          │  │  Cursor windows) │  │
                                          │  └──────────────────┘  │
                                          │  ┌──────────────────┐  │
                                          │  │  CoreGraphics    │  │
                                          │  │  (simulate Right │  │
                                          │  │  Option + Enter) │  │
                                          │  └──────────────────┘  │
                                          │  ┌──────────────────┐  │
                                          │  │  CGEventTap      │  │
                                          │  │  (know when      │  │
                                          │  │  Wispr stops     │  │
                                          │  │  typing)         │  │
                                          │  └──────────────────┘  │
                                          └────────────────────────┘
                                                     │
                                                     ▼
                                            ┌──────────────────┐
                                            │   Wispr Flow     │
                                            │   (activated by  │
                                            │   Right Option)  │
                                            └──────────────────┘
```

When you hold on the phone:

1. Server focuses the currently-selected Cursor window (AppleScript).
2. Server presses and holds **Right Option** — Wispr Flow's activation
   hotkey.
3. You talk. Your AirPods send audio to your Mac. Wispr transcribes.
4. You release on the phone. Server releases Right Option.
5. A global `CGEventTap` watches keystrokes. When Wispr has been quiet for
   400ms, the server fires Enter.

---

## Requirements

- **macOS** (Apple Silicon or Intel). Uses AppleScript, CoreGraphics,
  CGEventTap — all macOS-only APIs.
- **Python 3.10+** (3.11 or newer recommended).
- **[Cursor](https://cursor.com)** — this is built around focusing Cursor
  windows. Could be adapted to any app with light changes.
- **[Wispr Flow](https://wisprflow.ai)** — or any dictation tool that
  activates on a hold-to-talk hotkey and types into the focused window.
- **A phone** on the same Wi-Fi network. Any phone with a modern browser
  (iOS Safari / Android Chrome).

---

## Setup

> Tip: you can run every command below by pasting it into Terminal.

### 1. Clone and install

```bash
git clone https://github.com/<you>/hand-control.git
cd hand-control
./run.sh
```

`run.sh` will:

- Create a Python virtualenv in `.venv/`
- Install dependencies (`fastapi`, `uvicorn`, `pyobjc`)
- Start the server on port **8000**

Leave it running. The first start will print something like:

```
Hand Control running.
  On your phone, open:  http://192.168.1.42:8000
```

You'll come back to this URL shortly.

### 2. Configure Wispr Flow

Open Wispr Flow settings and change:

- **Activation hotkey** → `Right Option` (the Option/Alt key on the right
  side of your keyboard).
- **Input device** → your AirPods, or "System default" if your AirPods are
  already the Mac's default mic.

The Right Option key is used because (a) it's a single physical key,
perfect for press-and-hold, (b) it's rarely bound to anything else, and
(c) unlike `Fn`, it can be cleanly simulated programmatically.

### 3. Grant macOS permissions

The server simulates key presses and watches global keystrokes, so it
needs two permissions.

#### A. Accessibility

> **System Settings → Privacy & Security → Accessibility**

Enable the app you launched `./run.sh` from (Terminal.app, iTerm, or
Cursor's built-in terminal).

If you don't see it listed, try running `./run.sh` once first — macOS may
prompt you automatically.

After granting, **restart the server** (`Ctrl+C` and `./run.sh` again).

#### B. Automation → System Events

The first time the server lists your Cursor windows, macOS will prompt:

> "Terminal.app wants access to control System Events."

Click **OK**. (If you accidentally click Don't Allow, fix it at
**System Settings → Privacy & Security → Automation** → your terminal →
enable "System Events".)

### 4. Open the phone UI

On your phone's browser, go to the URL the server printed. For example:

```
http://192.168.1.42:8000
```

Hold your phone in **landscape**. Optionally tap Share → "Add to Home
Screen" for an app-feel icon.

---

## Using it

The phone UI has three zones:

```
┌─────────────────────────────────────────────────────┐
│ ●  [ project-a ] [ project-b ] [ project-c ]        │   ← top strip: tap to pick
├─────┬─────────────────────────────────────────┬─────┤
│     │                                         │     │
│  ◀  │           HOLD TO TALK                  │  ▶  │
│     │       (pulses while holding)            │     │
│     │                                         │     │
└─────┴─────────────────────────────────────────┴─────┘
  prev          press-and-hold zone               next
```

- **Top strip** — one box per open Cursor window. Tap to select.
- **Left edge (◀)** — select previous window.
- **Right edge (▶)** — select next window.
- **Center area** — press and hold to dictate. Selecting any window also
  raises that Cursor window to the front on your Mac, so you always know
  which one you're about to dictate to.

When you release the center, Wispr finishes typing, and Enter fires
automatically.

---

## Configuration

### Change the Enter delay

Edit `server/main.py`:

```python
ENTER_IDLE_MS = 400     # how long Wispr must be quiet before Enter fires
ENTER_MAX_WAIT_S = 8.0  # safety cap — fires Enter no matter what after this
```

### Change the activation hotkey

Edit `server/key_control.py` — change `KEYCODE_RIGHT_OPTION` to a different
keycode (see
[this list](https://eastmanreference.com/complete-list-of-applescript-key-codes))
and set the matching hotkey in Wispr Flow's settings.

### Change the target app

Edit `server/cursor_windows.py` — replace `"Cursor"` in the AppleScript
with another app name (e.g., `"Code"` for VS Code, `"iTerm2"` for iTerm).

### Control a different hold-to-talk tool

The server is Wispr-agnostic. Any dictation tool that activates on a hold
hotkey and types into the focused window works.

---

## Troubleshooting

**The server prints `Failed to create event tap`.**
Accessibility permission isn't granted, or the terminal binary that's
actually running Python doesn't have it. Check System Settings →
Accessibility, make sure your terminal app is enabled, then restart the
server. If Enter never fires, the event tap isn't running — the hard cap
(`ENTER_MAX_WAIT_S`) will still eventually fire Enter.

**Phone shows "No Cursor windows detected".**
Either Cursor isn't running, or Automation permission for System Events
isn't granted. Open **System Settings → Privacy & Security → Automation**
and enable "System Events" under your terminal.

**Phone can't reach the URL.**
Make sure both devices are on the same Wi-Fi. Some guest / corporate
networks isolate clients — try a personal hotspot to confirm. macOS
firewall may also need to allow incoming connections to Python.

**Holding works but Wispr doesn't start.**
Wispr Flow isn't running, or its hotkey isn't `Right Option`. Double-check
the hotkey in Wispr's settings.

**Enter fires too early or too late.**
Adjust `ENTER_IDLE_MS` in `server/main.py`. For slower / longer
transcriptions, try 600–800ms.

**The server kills any held modifier on disconnect.**
Intentional. If your phone drops Wi-Fi mid-hold, we release Right Option
so you aren't stuck with a modifier pressed forever.

---

## Project structure

```
server/
  main.py                  FastAPI app, WebSocket endpoint, orchestration
  cursor_windows.py        AppleScript: list + focus Cursor windows
  key_control.py           CoreGraphics: simulate Right Option + Enter
  keystroke_watcher.py     CGEventTap: detect when Wispr stops typing
phone/
  index.html               Single-file landscape remote UI
requirements.txt
run.sh                     One-shot bootstrap + run script
```

No build step, no frontend framework, no database. Single
`./run.sh` to go from fresh clone to working remote.

---

## Security notes

- The server binds to `0.0.0.0:8000` so your phone on the LAN can reach
  it. Anyone on the same Wi-Fi network can also reach it — including
  anyone who could type keys into your Cursor windows through it.
  Run on trusted networks only.
- There is no authentication. This is a tool for your own laptop.
- All audio stays on your Mac. The phone never records or transmits
  audio.

If you want to expose this beyond your LAN, put it behind Tailscale — it
"just works" and adds authentication and encryption for free.

---

## License

MIT. See [LICENSE](./LICENSE).
