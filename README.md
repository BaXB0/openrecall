```
   ____                   ____                  ____   
  / __ \____  ___  ____  / __ \___  _________ _/ / /   
 / / / / __ \/ _ \/ __ \/ /_/ / _ \/ ___/ __ `/ / /    
/ /_/ / /_/ /  __/ / / / _, _/  __/ /__/ /_/ / / /     
\____/ .___/\___/_/ /_/_/ |_|\___/\___/\__,_/_/_/      
    /_/                                                                                                                         
```
**Enjoy this project?** Show your support by starring it! ⭐️ Thank you!

## About this fork (a fork of OpenRecall)

This repository is a **personal fork of [openrecall/openrecall](https://github.com/openrecall/openrecall)**, reworked to run natively on **Arch Linux + KDE Plasma (Wayland)** and extended with a set of features on top of upstream. Everything lives on the single **`main`** branch: this fork = upstream `main` + one commit of fork changes; upstream is synced by merging `upstream/main` into it.

| | |
|---|---|
| **Upstream** | [`openrecall/openrecall`](https://github.com/openrecall/openrecall) — AGPLv3 |
| **This fork** | [BaXB0/openrecall](https://github.com/BaXB0/openrecall) |
| **Working branch** | **`main`** — the only branch; the fork = upstream + the changes below |
| **Upstream sync** | merge `upstream/main` in (`upstream` is fetch-only; pushes to it are blocked) |
| **Full technical delta & merge procedure** | [CUSTOMIZATIONS.md](CUSTOMIZATIONS.md) |

If you just want unmodified upstream OpenRecall, use the upstream install instructions further down and ignore this section.

### What changed vs upstream

**Platform — Linux-native (Arch Linux + KDE Plasma Wayland)**
- New `openrecall/wayland/` backend replacing the Win32 window/monitor code: window + monitor enumeration over KWin DBus (KRunner window runner + `KWin.getWindowInfo`), monitor geometry from `kscreen-doctor` (`pos`+`size`, mirror outputs skipped, `OPENRECALL_IGNORE_MONITORS`), a KWin-**script** active-window bridge (`org.openrecall.KWinBridge`) since KWin exposes no focus over DBus, virtual desktops, idle time, plus wlroots/X11 fallbacks and compositor auto-detection.
- Screenshots via full-screen `spectacle -f -b -n -o` split per monitor by geometry (KWin's `ScreenShot2` DBus API refuses non-Spectacle callers).
- Correct top-window attribution: windows are ordered *topmost-first* (not-minimized → focused → fullscreen → higher layer) so a **fullscreen game wins its monitor** over a background browser; the stored `windows` JSON carries per-window `active`/`fullscreen` flags.
- Cross-platform browser tab capture: a dispatcher (`browser_url.py`) with a Firefox/LibreWolf Remote-Debugging implementation on Linux and the original UI Automation one on Windows.
- Rich Linux data sources (`linux_sources.py`): PipeWire (screen-share + call detection), KActivities and journald → `linux_snapshots.jsonl`.
- **Process model**: the Windows tray supervisor, `.bat` and `.vbs` wrappers are gone; replaced by **`systemd --user` units** — `openrecall-capture.service` (the only capture process), `openrecall-web.service` (web-only, `OPENRECALL_DISABLE_CAPTURE=1`, so the two never double-capture), `openrecall-ai.service` + `.timer`, and `ollama.service`. `tray.py` remains as an **optional** StatusNotifierItem UI. `setup_openrecall.sh` creates `.venv`, installs pinned deps and generates the units + `.sh` wrappers.
- Storage default follows each platform's convention (Linux: `~/.local/share/openrecall`); override with `OPENRECALL_STORAGE` or `--storage-path`.

**Capture pipeline**
- Multi-monitor **"moments"**: all monitors captured under one shared timestamp → one DB row + one `.webp` per monitor; only screens that actually changed get a fresh screenshot/OCR/embedding, the rest reuse their last file.
- Session-friendly capture: a looser change threshold (`OPENRECALL_SIMILARITY_THRESHOLD`, default 0.75) so a stray toast doesn't start a session; a 5-minute heartbeat while active so long reading/movie blocks stay one session; while AFK only real screen changes write a moment, so a static overnight desktop yields nothing and the resulting 2h gap cleanly ends the session.
- Automatic once-a-day pruning of old moments the AI never summarized.

**Data model / database**
- Schema extended with `filename`, `monitor`, `screen_x`, `browser_url`, `browser_tabs`, `windows`; `UNIQUE(timestamp, monitor)`; in-place auto-migration for old DBs; connections closed explicitly.
- Per-**screen** OS context: the active (on-top) tab of every visible browser window (title + URL + monitor), every visible window's app/title/monitor, and the per-screen top window used as that row's `app`/`title`.
- **Session/day model** (`sessions.py`): a "day" is a run of moments with no gap > `SESSION_GAP_HOURS` (default 2h), keyed by its local start date (`_2`/`_3` for repeats on the same date), with a micro-run filter (`SESSION_MIN_LEN_MINUTES`) so a stray notification can't create a day.

**Web UI**
- Rebuilt from a single slider into a day calendar (48-segment activity strips), a per-day multi-monitor "trio" viewer with per-screen AI descriptions, collapsible capture details, a per-hour schedule strip, and improved search; new JSON APIs (`/api/timeline`, `/api/day/<day>`), a live AI-progress page (`/ai`), and an **Ask** page (`/ask`).

**AI summaries (local, via Ollama)**
- `bulk_summary.py`: per-moment vision descriptions + per-day markdown narrative. The vision model is a *thin* synthesizer (OS context is already stored, so it never transcribes windows/tabs); the heavy writer produces only DAY OVERVIEW + PATTERNS, while the SCHEDULE is assembled in code from real timestamps (exactly one `[no data …]` line per real gap, no invented hours) and emitted both into `<day>.md` and `<day>_schedule.json`. Hours can request extra on-screen moments (`REQUEST_MORE`, capped); `--no-today` skips still-open sessions; days are **sessions**.
- Wallpaper is a **scripted-only** concern: screens matching `wallpaper_reference.webp` (≥ `WALLPAPER_SIM_THRESHOLD`, default 0.90) or with no window attributed are never sent to the model; their placeholder text is written in code. `wallpaper_calibrate.py` tunes the threshold.
- Shared live progress parser (`ai_status.py`) drives both the web UI and the optional tray.

**Search / Ask**
- `openrecall/ask.py`: plain-English questions ("when did I last play osu?") answered over the AI-summary corpus using hybrid BM25 + MiniLM retrieval with an optional local Ollama rerank, grouped into "sittings" with deep links back into the timeline.

**Removed**
- All Windows `.bat`/`.vbs` launchers and the Windows tray supervisor (superseded by the systemd services).

# Take Control of Your Digital Memory

OpenRecall is a fully open-source, privacy-first alternative to proprietary solutions like Microsoft's Windows Recall or Limitless' Rewind.ai. With OpenRecall, you can easily access your digital history, enhancing your memory and productivity without compromising your privacy.

## What does it do?

OpenRecall captures your digital history through regularly taken snapshots, which are essentially screenshots. The text and images within these screenshots are analyzed and made searchable, allowing you to quickly find specific information by typing relevant keywords into OpenRecall. You can also manually scroll back through your history to revisit past activities.

## Features (this fork)

In addition to upstream's snapshotting + semantic search, this fork adds:

- **Linux-native (Arch Linux + KDE Plasma Wayland)** — window, active-window and monitor capture via KWin DBus plus a KWin-script focus bridge, Spectacle-based screenshots, extra data sources (PipeWire, KActivities, journald) and a `systemd --user` service stack (`openrecall-capture`, `openrecall-web`, `openrecall-ai`) instead of the Windows tray launcher.
- **Multi-monitor "moments"** — every capture records all monitors under one timestamp, shown side-by-side in the web UI.
- **OS-verified context per screen** — the active (on-top) tab of every visible browser window and the title of every open window are read directly from the OS and stored **per monitor**, so background tabs and pages with too-small-to-OCR text are still recorded precisely. Only the tab actually in view is captured (one per window), so even 50+ open tabs stay cheap.
- **Local AI day summaries** (`bulk_summary.py`, via Ollama) — per-moment vision descriptions enriched with that OS context, rolled up into a readable per-day markdown narrative.
- **"Ask" your history** (`openrecall/ask.py`) — plain-English questions ("when did I last play osu?") answered from the AI summaries, with deep links back into the timeline.
- **Automatic pruning** (`prune.py`) — removes old moments the AI never summarized so disk stays bounded while keeping your summarized history.
- **Optional tray launcher** (`tray.py`) + console snapshot tools (`snapshot.py`) alongside the web UI — the tray also shows **live AI-summary progress** (current day + stage, vision moments done/total, and the latest scene the model output) in its tooltip and an "AI Summary" submenu, raises a balloon when each day's summary finishes, and can **start/stop** a bulk-summary run right from that submenu.

See [CUSTOMIZATIONS.md](CUSTOMIZATIONS.md) for details.

## Quick start (this fork / Linux — Arch + KDE Plasma Wayland)

```
./setup_openrecall.sh          # one-time: create .venv + pinned deps + $HOME systemd units
# Start the stack (systemd --user):
systemctl --user enable --now openrecall-capture.service openrecall-web.service
systemctl --user enable --now openrecall-ai.timer           # optional: scheduled AI summaries
# then open http://localhost:8082

./snapshot.sh                  # one manual capture
./bulk_summary.sh              # manual: summarize sessions that lack an AI summary
./prune.sh --dry-run           # preview old-image pruning (--days N to change window)
```

The capture loop and the web server run as **separate** services so they never double-capture.
Storage defaults to `~/.local/share/openrecall` (the platform convention); override with `OPENRECALL_STORAGE` or `--storage-path`.

Requirements: KDE Plasma on Wayland (KWin window/monitor capture + Spectacle screenshots), `qdbus6`, `kscreen-doctor`, `spectacle`, and Ollama for AI summaries. See [CUSTOMIZATIONS.md](CUSTOMIZATIONS.md) for the full delta list and `AGENTS.md` for developer notes.

## Why Choose OpenRecall?

OpenRecall offers several key advantages over closed-source alternatives:

- **Transparency**: OpenRecall is 100% open-source, allowing you to audit the source code for potential backdoors or privacy-invading features.
- **Cross-platform Support**: OpenRecall works on Windows, macOS, and Linux, giving you the freedom to use it on your preferred operating system.
- **Privacy-focused**: Your data is stored locally on your device, no internet connection or cloud is required.

## Features

- **Time Travel**: Revisit and explore your past digital activities seamlessly across Windows, macOS, or Linux.
- **Local-First AI**: OpenRecall harnesses the power of local AI processing to keep your data private and secure.
- **Semantic Search**: Advanced local OCR interprets your history, providing robust semantic search capabilities.
- **Full Control Over Storage**: Your data is stored locally, giving you complete control over its management and security.

## Comparison



| Feature          | OpenRecall                    | Windows Recall                                  | Rewind.ai                              |
|------------------|-------------------------------|--------------------------------------------------|----------------------------------------|
| Transparency     | Open-source                   | Closed-source                                    | Closed-source                          |
| Supported Hardware | All                         | Copilot+ certified Windows hardware              | M1/M2 Apple Silicon                    |
| OS Support       | Windows, macOS, Linux         | Windows                                          | macOS                                  |
| Privacy          | On-device, self-hosted        | Microsoft's privacy policy applies               | Connected to ChatGPT                   |
| Cost             | Free                          | Part of Windows 11 (requires specialized hardware) | Monthly subscription                   |

## Get Started

### Prerequisites
- Python 3.11
- MacOSX/Windows/Linux
- Git

To install:
```
python3 -m pip install --upgrade --no-cache-dir git+https://github.com/openrecall/openrecall.git
```

To run:
```
python3 -m openrecall.app
```
Open your browser to:
[http://localhost:8082](http://localhost:8082) to access OpenRecall.

## Arguments
`--storage-path` (default: user data path for your OS): allows you to specify the path where the screenshots and database should be stored.

`--primary-monitor-only` (default: False): only record the primary monitor (rather than individual screenshots for other monitors)

## Uninstall instructions

To uninstall OpenRecall and remove all stored data:

1. Uninstall the package:
   ```
   python3 -m pip uninstall openrecall
   ```

2. Remove stored data:
   - On Windows:
     ```
     rmdir /s %APPDATA%\openrecall
     ```
   - On macOS:
     ```
     rm -rf ~/Library/Application\ Support/openrecall
     ```
   - On Linux:
     ```
     rm -rf ~/.local/share/openrecall
     ```

Note: If you specified a custom storage path at any time using the `--storage-path` argument, make sure to remove that directory too.

## Contribute

Found a bug or want a feature? Open an **issue** — or better, a **pull request** — in this repository: https://github.com/BaXB0/openrecall/issues

For problems with *unmodified upstream* OpenRecall, or to contribute changes back to the original project, use [openrecall/openrecall](https://github.com/openrecall/openrecall) instead.

## License

OpenRecall is released under the [AGPLv3](https://opensource.org/licenses/AGPL-3.0), ensuring that it remains open and accessible to everyone.

