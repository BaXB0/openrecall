# Fidelity & Search Roadmap (OpenRecall fork)

Status: brainstorm / scratchpad. Every idea below is tagged with the current
behavior it would change, so nothing is lost while the pipeline is fast enough
(measured: full 496-moment day = 5.4 min) to afford adding fidelity.

## 1. Why things are coarse today (facts + measured)

| Knob | Current | Effect |
|---|---|---|
| Sample density | `--sample-min 20` → ~1 moment / 20 min | 42 of ~500 moments get described; hour blocks ~25-30 min |
| Vision image size | 640 px JPEG | small window titles not readable by VL model |
| Capture cadence | 1 moment / 60 s (screenshot + OCR + embedding) | windows open <60 s never recorded |
| Core OCR | openrecall-bundled OCR | user verdict: "sucks"; feeds only search embeddings |
| AI output storage | `summaries/*.md` + `*_vision.json` only | NOT in recall.db → **not searchable by the app** |
| Search | cosine similarity over OCR `text` embeddings | no exact match, no `program=` style filters, no day/time range |

## 2. Capture / vision fidelity

- **I1 — Fixed 15-min sample grid.** Replace the evenly-but-loose `sample_min`
  picking in `batches_for_day` with a deterministic grid (e.g. every 15 min,
  choosing the batch nearest each grid tick). Hour blocks shrink to ~7-15 min
  and quick app switches stop falling between samples. Cost: ~120 calls/day vs
  42 (+8-10 min/day at current speed). Dense days get exact-catch coverage.
- **I2 — Vision context = window relationships.** Today the OS hint passes only
  per-screen `app/title/url`. Feed the existing `windows` JSON (already
  top-to-bottom z-order) so the model understands stacking/occlusion: which
  window is on top, what is hidden behind, what is minimised. "How do the
  windows relate to each other" is currently invisible to vision.
- **I3 — Bigger vision frames when it matters.** Optional `--vision-max` 640→1024.
  Only +0.5 s/moment; buy better small-text reading where the user cares.
- **I4 — OCR that isn't the bundled one.** RapidOCR (Paddle/onnx) or Tesseract
  as an opt-in second channel → feed exact visible text into vision+writer
  prompts as ground truth, plus into search. Bundled OCR stays for cheap search.
- **I5 — Capture cadence 30 s.** Catches transient windows; no AI-cost change
  (sampling caps it); ~2× rows/storage.
- **I6 — Background tabs, current-desktop only.** `browser_tabs` is already
  virtual-desktop-filtered and active-tab-only. Extend to
  `browser_tabs_all` = titles+URLs of every open tab (still current desktop).
  Writer then knows the session context ("Reddit + terminal + 3 fans on all day").

## 3. AI output ↔ search integration (the big one)

Goal: "everything we gather with AI must be queryable" — **make AI-derived text
searchable in the app**, and give OpenClaw/other agents a stable way to read it.

- **S1 — Backfill scenes into the database.** On summary, upsert each sampled
  moment's scene description (`[HH:MM] M0/M1/M2 + FOCUS + TAGS`), the hour
  narrative, and the day overview into recall.db as a structured, searchable
  layer:
  - new `scenes` table (timestamp, monitor/index, scene_text, tags, source),
    or a `w_`-prefixed column set on `entries`; both cheap on SQLite.
  - index with embeddings (AI description embeds better than OCR text) so
    *semantic* search starts matching "what I was doing", not just OCR crumbs.
- **S2 — Hybrid ranking.** Rank = blend of (a) OCR cosine, (b) scene-text
  cosine, (c) BM25/keyword hits. Keyword hits on `app`/`title`/`browser_url`
  near the top.
- **S3 — Search syntax (user examples):**
  - exact / "for direct search": quote or `exact:` mode → literal substring
    match, e.g. `exact:"day selection bug"`.
  - field filters: `program=discord` (matches `app`), `app=`, `monitor=`,
    `day=2026-09-08`, `time>02:00&time<03:00`, `url=`, `tag=GAMING`,
    `scene=edging`.
  - free text + filters combine: `term program=discord`.
- **S4 — Time-clustered timeline results.** Visualize search hits along the day
  clock (per current day-view), dedupe unchanged repeated screens, collapse
  reuse runs.
- **S5 — `/api/scenes` + richer `/api/search`.** JSON endpoints so OpenClaw (or
  any agent/skill) can query memory: `GET /api/scenes?day=&q=&tag=` and
  `GET /api/search?q=&exact=&program=&from=&to=`. Stable schema = the
  "communication" contract between capture-AI and OpenClaw.

## 4. OpenClaw ↔ OpenRecall communication

- **Read back:** OpenClaw asks "what was I doing around 2am last Tuesday" →
  `/api/scenes` or `/api/search`, gets timestamped scene text + images. Needs
  the endpoints above plus a small doc/example (curl or MCP).
- **Push:** optionally write finished day `.md`/scenes into OpenClaw's memory
  store (exact mechanism TBD once we know how OpenClaw stores memory), or leave
  it read-only pull.
- **Registry / discoverability:** keep the scene/summary JSON schema in one
  module shared by both sides so a schema change can't silently desync OpenClaw.

## 5. Suggested order of work

1. I1 (15-min grid) — pure knob, immediate timeline fidelity up.
2. I2 (windows z-order → vision) — no cost, better "how windows relate".
3. S1 + S3 + S5 (scenes in DB + filters + /api) — unlocks OpenClaw comms.
4. S2 hybrid ranking + S4 timeline results — search quality.
5. I4 (RapidOCR) — escape the "sucks" OCR, feed search + vision.
6. I5/I6 opt-in capture depth.