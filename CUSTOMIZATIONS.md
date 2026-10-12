# Local customizations (this checkout) vs upstream openrecall

This repository is a **working copy of upstream [`openrecall/openrecall`](https://github.com/openrecall/openrecall)** with a set of local modifications committed on a **`local` branch**.

- `main`  — clean, tracks `origin/main`, always matches upstream.
- `local` — carries all customizations (the current working branch).

The purpose of this file: know exactly what is different, which systems are impacted, and how to keep pulling upstream git updates without losing your local changes.

---

## What changed and which systems are impacted

### Modified tracked files (would conflict on `git pull` if upstream touches them)

| File | What was changed | System(s) impacted |
|---|---|---|
| `openrecall/screenshot.py` | Capture loop rewritten: grabs all monitors each cycle, writes one shared timestamp per "moment", one DB row per monitor; only changed screens get fresh screenshot/OCR/embedding; unaffected screens reuse the last saved file; captures the ACTIVE tab of every visible browser window + all visible window titles (best-effort OS context), **per monitor** -- each row stores only the tabs/windows/URL on THAT screen and the topmost window on that screen as its app/title; loops every ~60s; auto-prunes once/day. **Session-friendly timestamps**: the mutual-similarity bar is loosened (`OPENRECALL_SIMILARITY_THRESHOLD`, default 0.75) so a stray toast over a static screen does NOT count as a change (no phantom midnight-notification sessions); while the user is ACTIVE a heartbeat moment is written every 5 min even when nothing changed (long reading/movie blocks don't open false gaps and split a day); while AFK only real screen changes write a moment (a playing movie keeps being tracked; a static desktop overnight writes NOTHING, which is the 2h-gap the session detector reads as the day boundary) | **Capture pipeline**, **storage volume/size**, **multi-monitor data model**, **session/day boundaries** |
| `openrecall/database.py` | Schema extended with `filename`, `monitor`, `screen_x`, `browser_url`, `browser_tabs`, `windows` columns; unique constraint changed to `UNIQUE(timestamp, monitor)`; auto-migration via `ALTER TABLE` for old DBs; connections now explicitly closed (Windows file-lock fix); new queries `get_entries_for_day`, `get_days_with_entries`, `delete_entries_for_timestamp`; **and a session layer**: `get_sessions()` (gaps > `SESSION_GAP_HOURS` split the timeline into "days", keyed by the local START date, `_2`/`_3` suffixes for same-calendar-date repeats), `get_entries_for_session(key)` (both bounds inclusive) and the shared `get_entries_between(start,end,inclusive_end)` primitive | **Database schema & queries** (used by web UI + pruning + AI summaries) |
| `openrecall/app.py` | Web UI rewritten from a single slider into a full daily timeline: calendar, per-day "trio" viewer showing all monitors side-by-side, AI description rendering (incl. OS-supplied tabs/windows collapsibles), search improvements with empty-state handling, new JSON APIs (`/api/timeline`, `/api/day/<day>`), `image_url` → `filename` changes. For AI-described moments the built-in capture info (URL link / tabs / windows) is folded into a collapsible "Capture details" and replaced on each screen by the vision model's PER-MONITOR description; a per-hour **schedule strip** under the slider shows only the current hour's blocks as you scrub images; the static summary card shows only DAY OVERVIEW + PATTERNS (the SCHEDULE is no longer rendered there — it lives in the strip instead). **Sessions**: the timeline/navigation is now driven by `sessions[]` (a "day" = a run of moments with no >2h gap, keyed by its local start date, `_2` suffix for a second session on the same date); the calendar keeps its month grid but each day cell carries a 48-segment activity strip (green = active, `#e9ecef` = gap) reflecting the real minutes, cells open the session that started on that date (falling back to the session covering its midnight), and prev/next + the selected day walk through sessions, not calendar dates | **Web UI / frontend**, **search** |
| `openrecall/config.py` | Linux default storage follows the platform convention (`~/.local/share/openrecall`); adds an `OPENRECALL_STORAGE` env override, a new `summaries` subfolder, and `parse_known_args` so other tools can pass their own argv. **Session settings**: `SESSION_GAP_HOURS` (`OPENRECALL_SESSION_GAP_HOURS`, default 2.0) and `SESSION_MIN_LEN_MINUTES` (`OPENRECALL_SESSION_MIN_LEN_MINUTES`, default 10) | **Config / storage location**, **session boundaries** |
| `openrecall/utils.py` | Added Win32 window enumeration (`get_windows_windows`, `get_all_windows_as_list`) returning app + title + the 0-based `monitor` index each window sits on (resident on the current virtual desktop, ignoring tray/shell), per-monitor top windows (`get_top_window_per_monitor`) for per-screen active app/title, monitor geometry helpers (`monitor_rects`, `monitor_for_rect` mapping window rects → screens), `get_windows_osx` for macOS and `is_on_current_virtual_desktop(hwnd)` COM check. `get_top_window_per_monitor` assigns a window to the monitor it covers the MOST area on (≥5% of the monitor or of the window itself), NOT by where its centre falls, so a screen that is really just wallpaper no longer gets a phantom top window from a neighbouring screen's window; the same area-based rule (shared `monitor_for_rect_area` helper) is used for the per-screen `windows` list and browser tabs, so no column ghosts windows onto an empty-desktop neighbour | **OS context capture** (feeds `windows` column + AI prompts) |
| `tests/test_database.py` | Updated for new schema (per-monitor dedup + context columns), added tests for `get_days_with_entries` / `delete_entries_for_timestamp` | **Tests** |
| `tests/test_nlp.py` | Expects `cosine_similarity` to return `0.0` (not NaN) for zero-magnitude vectors | **Tests** |

### New untracked files (safe from upstream unless upstream adds the same filename)

| File | Purpose | System(s) added |
|---|---|---|
| `openrecall/browser_url.py` + `browser_url_linux.py` + `browser_url_win.py` | Active-browser-tab tracker. `browser_url.py` is now a cross-platform dispatcher: on Linux it drives **Firefox/LibreWolf Remote Debugging Protocol** (`browser_url_linux.py`; the browser must be started with remote debugging, e.g. `--start-debugger-server 6000`, otherwise it degrades to empty), on Windows it keeps the original **UI Automation** implementation (`browser_url_win.py`). `get_browser_tabs()` returns the ACTIVE (on-top) tab — title + URL + the 0-based monitor index the window sits on — of every visible browser window, one per window (never background tabs). Two-phase scan (bounded shallow pass + budgeted deep pass) so even 50+-tab windows stay fast; title/URL cleaning for Edge/LibreWolf quirks. `get_browser_url()` kept as a compat wrapper. Adds deps: `psutil` (`uiautomation` on Windows only) | **Browser-URL/tabs tracking** (feeds `browser_url` + `browser_tabs` columns, web UI links, AI prompts) |
| `openrecall/wayland/` (`__init__.py`, `kde.py`, `kde_active.py`, `kde_screenshot.py`, `wlr.py`, `x11.py`) | **Linux compositor backend.** `kde.py`: monitor geometry from `kscreen-doctor` (`pos`+`size`, mirror outputs skipped, `OPENRECALL_IGNORE_MONITORS`), window list via KRunner's window runner + `KWin.getWindowInfo`, active window, virtual desktops, idle. `kde_active.py`: a KWin **script bridge** that reports `workspace.activeWindow` over `callDBus` (DBus name `org.openrecall.KWinBridge`, single owner — the capture service). `kde_screenshot.py`: full-screen Spectacle grab split per monitor by geometry (`ScreenShot2` DBus is `NoAuthorized` for us). `wlr.py`/`x11.py`: wlroots and X11/XWayland fallbacks. `__init__.py`: compositor detection + unified `get_monitors/get_windows/get_active_window/get_virtual_desktops/get_idle_time` | **Linux capture backend** (replaces the Win32 enumeration) |
| `openrecall/linux_sources.py` | **Rich Linux data sources** feeding `linux_snapshots.jsonl`: KWin windows + virtual desktops, KActivities, PipeWire stream inspection (screen-share + in-call detection), user-session journal, and the Firefox native-messaging manifest helper. Window/virtual-desktop queries delegate to the Gio backend (python `dbus` is not installed, so the old `_dbus_call`/`evalScript` helpers are dead code paths) | **Extra OS context** for AI summaries |
| `openrecall/ask.py` + `openrecall/static/ask.js` + `openrecall/templates/ask.html` | **"Ask your history"**: plain-English Q&A over the AI-output corpus only (`summaries/` vision + schedule + day `.md`; the DB is deliberately not searched because pruning makes the summaries the durable record). Hybrid BM25 + MiniLM embedding retrieval, optional Ollama rerank, results grouped into "sittings" with deep links back into the timeline. Routes `/ask` + `/api/ask` | **Search / Q&A** |
| `setup_openrecall.sh` | Linux one-time setup: creates `.venv`, installs pinned deps, checks optional system packages, and writes the generated `.sh` launcher wrappers (`tray.sh`, `snapshot.sh`, `snapshot_loop.sh`, `bulk_summary.sh`, `prune.sh`, `wallpaper_calibrate.sh`) | **Setup / dependencies** |
| `tests/test_ask.py` | Tests for the Ask retrieval/tokenizer | **Tests** |
| `openrecall/sessions.py` | Session/day segmentation: a **session** is a maximal run of moments with no gap longer than `SESSION_GAP_HOURS` (default 2h — the AFK boundary the capture loop produces). `split_sessions()` (with `min_len_minutes` micro-run filter, default 10, so a stray toast/notification never starts a session), `session_by_key()`, `covering_session()` (maps a calendar cell → its owning session). Keys are the local start date, `2026-09-13_2` when a second session starts the same date; a session owns its start day regardless of how far past midnight it runs | **Session/day model** (feeds web UI calendar, day views, AI summaries, pruning) |
| `openrecall/prune.py` | Weekly-ish pruning: deletes screenshot images + DB rows for captured moments older than N days that never got an AI description; skips days `bulk_summary` hasn't touched; runs once/calendar-day via stamp file. **Session-aware**: a session is judged by the date of its LAST moment (a session running 13 Sep 18:00 → 14 Sep 06:00 is pruned on the 14th), and pruning deliberately computes sessions with the micro-session filter OFF so even a 3-minute day is eventually pruned instead of invisible | **Disk-space management** |
| `bulk_summary.py` | Ollama-driven local summary pipeline (`*-vision.json` per-moment descriptions + `summaries/YYYY-MM-DD.md` day narratives). Since the capture already stores exact OS context (active app/title, ACTIVE browser-tab title+URL per window, open window titles, OCR — all per screen), the vision model is now a THIN synthesizer: it gets a short prompt + the monitor screenshots and reports ONE BLOCK PER SCREEN (`M0 (PRIMARY (main)):`, `M1 (LEFT monitor (1)):`, ...) plus a single `FOCUS:` line and `TAGS:` — it never transcribes windows/tabs. Vision cache entries carry a `fmt` marker (2) so re-scanning invalidates old-format descriptions. The heavy 14B writer produces ONLY `DAY OVERVIEW` + `PATTERNS`; the SCHEDULE section is assembled IN CODE from the per-hour digests and the real captured timestamps: clean `## HH:00 - HH+1:00` hour markers, exactly one `[no data HH:MM-HH:MM]` line per real gap (nothing invented, no misleading hour-windows), and an honest `[active HH:MM-HH:MM]` row for captured-but-unsummarized stretches. The code-built schedule is written both into `<day>.md` and as structured `<day>_schedule.json` (per-hour blocks + no-data spans) for the web UI. One writer pass per hour; the writer may REQUEST_MORE extra on-screen moments if the samples miss something (≤2 extra per hour, ≤12 per day); only hours that gained a usable scene are re-analyzed; blank first-pass digests trigger midpoint gap-fill. New `--no-today` flag skips sessions still open (last moment within `SESSION_GAP_HOURS` of now) on auto runs. **Session-aware days**: `find_days_with_entries()` returns session keys (`2026-09-13`, `2026-09-13_2`, ...) and each `process_day` pulls `get_entries_for_session(key)` — vision/writer files are keyed the same way (`2026-09-13_2_vision.json`, `2026-09-13_2.md`). If a vision call returns an empty reply it retries up to 3 times with a short blunt prompt. Progress lines the tray monitors are unchanged (`Vision analyzing moment at HH:MM`, `Analyzing hour ...`). Screens whose capture row has NO app/title/URL (no window actually covers them, per the area-based attribution) are treated as **user's wallpaper**: excluded from the model call outright, the per-monitor block is written locally as `showing the user's wallpaper`, and an all-wallpaper moment is synthesized with ZERO model calls (both results cached so reused screens never re-enter the vision call). The wallpaper ART is never named, hinted, or described in the prompts (no `WALLPAPER_DESC`), and the model is instructed to never describe it either. | **AI summaries** (drives the descriptions shown in the web UI and what pruning keeps) |
| `snapshot.py` + `snapshot.bat` | One-shot / loop capture entry point (all monitors, shared timestamps, same logic as `screenshot.py`, incl. per-screen active tabs + visible-window context) | **Capture pipeline** (console-friendly alternative to the web app thread) |
| `snapshot_loop.bat` | `snapshot.py --delay 60` continuous-capture launcher | **Capture pipeline** |
| `prune.py` + `prune.bat` | CLI wrapper for `openrecall/prune.py` (`--dry-run`, `--days N`, `--force`). `prune_if_due` honors `OPENRECALL_DISABLE_PRUNE` (1/true/yes/on) to hold off the daily auto-prune | **Disk-space management** |
| `tray.py` + `tray.bat` | System-tray background launcher (`pystray`); keeps the app + web UI on 8082 running headless. Also monitors `bulk_summary` live via the shared status module (`openrecall.ai_status`): hover tooltip + an "AI Summary" submenu show the current day, stage (vision / writer-hour-analysis / overview), moments done/total, and the latest scene text, with balloon notifications when a run starts/stops and when each day's `.md` completes. The submenu also has **Start bulk summary** (pending days), **Start bulk summary (shutdown after)**, **Stop bulk summary**, **Open live AI progress** (opens `/ai` in browser), and log/summary folder entries — launching the child headless with stdout discarded (the child writes its own authoritative log). Rebuilds its native menu on status change (the win32 pystray backend only refreshes after item callbacks) | **Background lifecycle**, **AI summaries** |
| `openrecall/ai_status.py` | **Shared AI progress parser**: reads the single authoritative progress log (`logs/bulk_summary.out.log`) and `summaries/` folder to produce a live status dict used by both the tray and the web UI. Provides `read_status()`, `tail()`, `progress_log_path()`, `summaries_path`, `appdata_folder`, `stage_label()`, `tray_title()`, `summary_process_running()`. `bulk_summary.py` tees stdout/stderr to the progress log so every launcher (tray, `.bat`, shell) produces the same parseable output. | **AI summaries**, **Web UI**, **Tray** |
| `setup_openrecall.bat` | Creates the `.venv` and installs **pinned** dependencies (incl. `torch==2.8.0`, `uiautomation`, `psutil`, `pystray`, doctr OCR fork) | **Setup / dependencies** |
| `start_openrecall.bat` | Launches `.venv` python `-m openrecall.app` | **Run workflow** |
| `wallpaper_calibrate.py` | Interactive tkinter calibrator for `WALLPAPER_SIM_THRESHOLD`: shows captured screenshots (primarily the Left monitor), the user labels each "same" (looks like the wallpaper reference) or "different" (real content), and after every answer it recomputes the threshold that best separates the two groups (fewest prediction errors, ties broken toward the most robust margin) so the number converges. Reuses `wallpaper_similarity()` from `bulk_summary.py` verbatim | **Wallpaper filter tuning** |
| `tests/test_prune.py` | Tests for the automated pruning | **Tests** |
| `tests/test_sessions.py` | Tests for session/day segmentation (gap splitting, `_2` repeat-keying, micro-session filter, covering-session mapping, stock-trade-hour/pre-midnight real cases) | **Tests** |

### Effective behavior deltas (grouped by system)

1. **Capture / storage**
   - Every capture writes a "moment": all monitors share one timestamp; one row + one `.webp` per monitor (`<timestamp>_<monitor>.webp`). Upstream saved one screenshot per change.
   - **A "day" is now a *session* — a run of moments with no gap > `SESSION_GAP_HOURS` (default 2h).** The capture loop makes this non-arbitrary: while active, a heartbeat moment every 5 min keeps long reading/movie blocks contiguous; while AFK, only real screen changes write a moment, so a static overnight desktop produces nothing and the 2h gap cleanly ends the session. Stray notifications don't split sessions (surface-similarity bar loosened to 0.75 by default). Sessions are keyed by their local start date (`2026-09-13`, plus `_2`/`_3` for a second/third session on the same date), own their start day, and can span midnight.
   - Calendar cells are still per calendar date, but colored by the real activity minutes (48-segment strip: green = active, `#e9ecef` = gap) so you SEE the 2h gaps; clicking a cell opens the session that started that date (or the session covering its midnight if the cell is only a tail).
   - Each row also stores that screen's OS context: `browser_tabs` (the ACTIVE tab, title+URL+screen, of every visible browser window — one per window, so 50+ open tabs cost one entry each), `windows` (every visible window's app+title+screen, with minimised ones kept on the leftmost screen's row), and `browser_url` only when a browser window covers that screen. Minimised/off-screen windows keep `monitor: null`.
   - The current sleep cadence is ~60s (vs upstream 3s), so moments are sparser but full-desktop-aligned.
   - Old DBs are migrated in place by `create_db()`; no data loss, new columns just appear.
2. **Web UI**
   - Complete rebuild: day calendar + per-day slider over "trio" (simultaneous multi-monitor) moments, plus AI description panels, tags, and a search screen showing app/title/text. Each screen image shows ITS OWN active-tab URL link (only when a browser covers that screen) and a "Focus tabs / Windows on this screen" block instead of repeating desktop-wide data on every monitor. `/api/timeline` + `/api/day/<day>` power it. Search now handles empty/missing-embedding cases.
   - Once a moment is AI-described (per-monitor format), each screen shows ITS OWN monitor's VISION text directly under that monitor's image (M0's description under M0, never combined into one block); the captured URL/tabs/windows collapse into a "Capture details" toggle. Undescribed moments show the raw capture info as before.
   - The **schedule is the big full-width text panel inside each moment view**: while browsing images it shows only the *current hour's* writer output (`Schedule 02:00 - 03:00` while viewing anything 02:00–02:59), including a `[no data HH:MM-HH:MM]` line when that hour touches a real gap. It hides when a day has no schedule.
   - The static day-summary card above the slider shows only DAY OVERVIEW + PATTERNS (the SCHEDULE section is never rendered there).
3. **Privacy / storage location**
   - Linux storage follows the platform convention (`~/.local/share/openrecall`); override with `OPENRECALL_STORAGE` or `--storage-path`.
4. **AI descriptions & pruning (new, interdependent)**
   - `bulk_summary.py` (manual, run when done for the day) uses Ollama locally: samples one moment per ~15 min, synthesizes each multi-monitor moment with a THIN vision prompt (what is happening, exact apps/sites/games seen on screen), then writes the day narrative.
   - The heavy writer is fed per-hour **bundles**: the vision scene descriptions PLUS the exact OS-verified context already stored per moment — a minute-level activity rollup of the primary screen (every timestamp of the hour, consecutive repeats collapsed) and per-sampled-moment app/title, active-browser-tab, open-window and OCR context. So "what was open in the background" reaches the report without OCR guessing or vision transcription. One writer pass per hour; if it needs on-screen visual detail between samples it may end its reply with a `REQUEST_MORE: HH:MM` line (≤2 per hour, ≤12 per day) and the pipeline loads vision once more with the same thin prompt to describe those extra moments, then re-analyzes only the hours that actually gained a usable scene. Blank first-pass digests trigger midpoint gap-fill to rescue empty hours. A full day finishes well within an hour.
   - Progress is visible from the system tray (`tray.py`): the icon tooltip and "AI Summary" submenu live-tail `logs/bulk_summary.out.log` + the `summaries/` folder for the current day, stage (vision / writer-hour-analysis / overview), moments done/total and the latest scene (see the tray row above). The same submenu can **start** a run (pending days or `--all`) and **stop** it, without needing `bulk_summary.bat`.
   - Pruning keeps any session that hasn't been summarized yet; a session is old when the date of its LAST moment falls before the cutoff (so a night through 6am is judged on its end day and doesn't linger an extra day). The micro-session filter is disabled for pruning so even tiny days are eventually pruned. After a week it removes the moments the AI **didn't** sample — so you end up with summarized moments + daily `.md`, not every raw screenshot. Setting `OPENRECALL_DISABLE_PRUNE=1` turns the auto-prune off completely (kept off while a full-summary rebuild is in flight so regen never fights with pruning).
   - **Wallpaper screens are a scripted-only concern** — the AI is never told about wallpaper and no wallpaper wording reaches any prompt. A screen whose screenshot looks like `wallpaper_reference.webp` (reference copy of the desktop art, kept at the data root) at >= `WALLPAPER_SIM_THRESHOLD` (default 0.90), or one with no window/URL attributed to it, is treated as "empty desktop": it is never sent to the vision model, and its `M<idx>:` line (`Screen contains no meaningful content.`) — plus the all-empty `IDLE_FOCUS`/`IDLE_TAGS` scene — are written in code. The comparison uses the SAME metric the capture loop applies (`mean_structured_similarity_index` MSSIM, extracted to `openrecall/similarity.py`), run at 192x108. `wallpaper_calibrate.py` interactively tunes the threshold against the one shared metric `bulk_summary.wallpaper_similarity()`; the number it recommends is exactly what you set `WALLPAPER_SIM_THRESHOLD` to.
   - Data format consumed by the UI: `summaries/<day>_vision.json` (per-moment, per-monitor `M<idx>` blocks, `fmt: 2`), `summaries/<day>_schedule.json` (per-hour blocks + real no-data spans), and `summaries/<day>.md` (SCHEDULE built in code + writer DAY OVERVIEW/PATTERNS).
5. **Dependencies added by this fork**
   - `psutil` (browser/context), `pystray` (optional tray), `python-doctr` (OCR fork). On Windows also `uiautomation`. Pinned versions live in `setup_openrecall.sh` (Linux).

---

## Linux port (Arch Linux + KDE Plasma Wayland)

This fork is now **Linux-native**. The original Win32/`.bat`/`.vbs` pieces were replaced with a KDE/KWin + `systemd` implementation; the Linux-specific delta in one place:

### Capture backend
- **Win32 out, KWin in.** All window / monitor / active-window capture goes through `openrecall/wayland/` (KDE + wlroots + X11 dispatch). `openrecall/utils.py` keeps its cross-platform entry points but the Linux path delegates to the wayland backend.
- **Screenshots**: full-screen `spectacle -f -b -n -o`, then split per monitor by geometry — the `org.kde.KWin.ScreenShot2` DBus API returns `NoAuthorized` for non-Spectacle callers.
- **Active window**: KWin exposes no focus over DBus, so a small KWin script reports `workspace.activeWindow` back via `callDBus` to `org.openrecall.KWinBridge` (`openrecall/wayland/kde_active.py`). Single owner — the capture service.
- **Top window per screen**: KWin's window list has no z-order, so `get_windows_kde()` sorts *topmost-first* (not-minimized → focused → fullscreen → higher layer) before `get_top_window_per_monitor()` keeps the first window covering each screen. This is what makes a **fullscreen game win its monitor** over a background browser.
- **Monitors**: `kscreen-doctor -j` (read `pos` + `size`; skip `replicationSource` mirrors; `OPENRECALL_IGNORE_MONITORS` drops by name).
- **Extra context**: `openrecall/linux_sources.py` adds PipeWire / KActivities / journald. Python `dbus` is **not** installed here, so its DBus-backed helpers are optional no-ops; window + virtual-desktop data goes through the Gio backend instead.

### Process model
- The Windows tray launcher is replaced by **`systemd --user` units**: `openrecall-capture.service` (the only capture process, `python -m openrecall.screenshot`), `openrecall-web.service` (web-only, `OPENRECALL_DISABLE_CAPTURE=1`), `openrecall-ai.service` + `.timer`, and `ollama.service`. `tray.py` survives as an **optional** StatusNotifierItem UI, not a supervisor.
- `.sh` wrappers generated by `setup_openrecall.sh` replace the old `.bat` wrappers.

### Known limitations
- **Browser tab capture needs opt-in**: it requires Firefox/LibreWolf started with remote debugging (`--start-debugger-server 6000`); otherwise `browser_tabs` is empty. A native-messaging manifest helper exists, but the webextension side is not shipped.
- The "Ask" corpus is the `summaries/` folder; a session that was never summarized is not searchable there.

---

## How to keep pulling upstream updates

Your customizations live on the **`local`** branch (`main` stays clean and tracks
upstream). When upstream releases something, merge it in:

```
git checkout main
git pull                       # or: git fetch origin && git merge --ff-only origin/main
git checkout local
git merge main                 # resolve conflicts in the modified files below if any
```

`git log --graph --decorate --oneline local main origin/main` shows the two lines
of history side by side so you can compare exactly what differs.

Conflicts are possible only in files upstream also changed — realistically the 6
modified files in the table above. Untracked/added files never conflict unless
upstream starts using the same filename.

### Things to watch when merging upstream

- `openrecall/screenshot.py`, `openrecall/app.py`, `openrecall/database.py`, `openrecall/config.py` are where upstream churn will collide with your deltas (biggest risk: upstream's own multi-monitor / schema work, which is on their roadmap).
- Upstream occasionally renames the DB columns or the screenshot filename scheme — search for `filename`, `monitor`, `browser_url`, `browser_tabs`, `windows`, `screen_x`, and `.webp` in the merge result.
- If upstream changes the OCR/embedding storage, the `np.frombuffer` `dtype` (float32) in `get_all_entries` is the place to check.

### Housekeeping note

`.venv/` is excluded via `.gitignore` (added alongside `venv`), so the virtualenv never gets staged.

---

## Quick start for this copy (Linux)

```
./setup_openrecall.sh          # one-time: create .venv + pinned deps + wrappers
systemctl --user enable --now openrecall-capture.service openrecall-web.service
# then open http://localhost:8082
./snapshot.sh                  # one manual capture
./bulk_summary.sh              # manual: summarize sessions that lack an AI summary
./prune.sh --dry-run           # preview old-image pruning (--days N to change window)
```