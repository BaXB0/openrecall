"""
Wallpaper-similarity threshold calibrator.

Shows you captured screenshots (primarily from the LEFT monitor, where the
wallpaper spends most of its time) and asks, for each one: does this screen look
like your empty desktop wallpaper, or is there real content on it?

  - click "Same"    (S) if the screen IS the wallpaper / empty desktop
  - click "Different" (D) if there is real content (windows, apps, pages)
  - click "Skip"    (Space) if you are not sure

After each answer the tool recomputes the best WALLPAPER_SIM_THRESHOLD so far --
the value that separates your "same" and "different" answers with the fewest
mistakes. As you label more screenshots the number converges (stabilizes).

Usage:
    .\\.venv\\Scripts\\python.exe wallpaper_calibrate.py [--samples N] [--monitor IDX]
"""
import argparse
import os
import sys
import tkinter as tk
from collections import Counter

from PIL import Image, ImageTk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from openrecall import config
from openrecall.database import create_db, get_all_entries
from bulk_summary import (
    WALLPAPER_REF_PATH,
    WALLPAPER_SIM_THRESHOLD,
    wallpaper_similarity,
)


def _best_threshold(scores, labels):
    """Threshold in [0,1] with the fewest prediction errors on the labels.

    "Wallpaper" is predicted when score >= t. Candidate thresholds are the
    midpoints between every pair of distinct observed scores (plus the extremes).
    Ties are broken toward the threshold furthest from the nearest labelled
    point of either class (most robust, not sensitive to a single edge sample).
    Returns (threshold, accuracy).
    """
    if not scores:
        return None, 0.0
    uniq = sorted(set(scores))
    cands = set()
    cands.add(0.0)
    cands.add(1.0)
    for a, b in zip(uniq, uniq[1:]):
        cands.add((a + b) / 2.0)
    best_t, best_err, best_margin = None, None, -1.0
    for t in cands:
        err = 0
        closest = float("inf")
        for s, lab in zip(scores, labels):
            pred = s >= t
            if pred != lab:
                err += 1
            else:
                closest = min(closest, abs(s - t))
        if best_err is None or err < best_err or (err == best_err and closest > best_margin):
            best_t, best_err, best_margin = t, err, closest
    return best_t, 1.0 - best_err / len(scores)


class Calibrator:
    def __init__(self, root, items):
        self.root = root
        self.items = items
        self.i = 0
        self.scores = []
        self.labels = []  # True == wallpaper (same), False == real content
        self.tk_img = None
        self._advance()

    def _advance(self):
        for child in self.root.winfo_children():
            child.destroy()
        if self.i >= len(self.items):
            self._finish()
            return
        meta = self.items[self.i]
        self.scores.append(wallpaper_similarity(meta["path"]))
        self.root.title(f"Wallpaper calibrator  {self.i + 1}/{len(self.items)}")
        frame = tk.Frame(self.root, bg="#1e1e1e")
        frame.pack(fill="both", expand=True)

        img = Image.open(meta["path"]).convert("RGB")
        iw, ih = img.size
        maxw, maxh = 1000, 560
        scale = min(maxw / iw, maxh / ih)
        if scale < 1:
            img = img.resize((max(1, int(iw * scale)), max(1, int(ih * scale))), Image.LANCZOS)
        self.tk_img = ImageTk.PhotoImage(img)
        tk.Label(frame, image=self.tk_img, bg="#1e1e1e").pack(pady=4)

        info = tk.Label(
            frame,
            text=(f"window: {meta['when']}   monitor: {meta['monitor']}"
                  f"   screen_x: {meta['screen_x']}"),
            fg="#c8c8c8", bg="#1e1e1e", font=("Consolas", 10),
        )
        info.pack()
        sim_label = tk.Label(
            frame,
            text=f"wallpaper similarity: {self.scores[-1]:.3f}",
            fg="#f0f0f0", bg="#1e1e1e", font=("Consolas", 14),
        )
        sim_label.pack(pady=(2, 0))

        btn_frame = tk.Frame(frame, bg="#1e1e1e")
        btn_frame.pack(pady=10)
        tk.Button(btn_frame, text="SAME  (S)", width=14, bg="#3a5a3a", fg="white",
                  command=self._same).pack(side="left", padx=8)
        tk.Button(btn_frame, text="DIFFERENT  (D)", width=14, bg="#5a3a3a", fg="white",
                  command=self._diff).pack(side="left", padx=8)
        tk.Button(btn_frame, text="SKIP (Space)", width=12, bg="#4a4a4a", fg="white",
                  command=self._skip).pack(side="left", padx=8)

        t, acc = _best_threshold(self.scores, self.labels)
        status = (f"labelled: {sum(self.labels)} same / "
                  f"{sum(1 for x in self.labels if not x)} different")
        if t is not None:
            status += f"\nbest threshold so far: {t:.3f}  (accuracy {acc:.0%})"
        tk.Label(frame, text=status, fg="#9ad6ff", bg="#1e1e1e",
                 font=("Consolas", 11)).pack(pady=(2, 0))

        self.root.bind("s", lambda e: self._same())
        self.root.bind("d", lambda e: self._diff())
        self.root.bind("<space>", lambda e: self._skip())

    def _push(self, label):
        self.labels.append(label)
        self.i += 1
        self._advance()

    def _same(self):
        self._push(True)

    def _diff(self):
        self._push(False)

    def _skip(self):
        self.labels.append(None)
        self.i += 1
        self._advance()

    def _finish(self):
        for child in self.root.winfo_children():
            child.destroy()
        good = [(s, l) for s, l in zip(self.scores, self.labels) if l is not None]
        t, acc = _best_threshold([s for s, _ in good], [l for _, l in good])
        lines = [
            "Calibration complete.",
            f"  total viewed: {len(self.items)}",
            f"  labelled:     {len(good)}",
            f"  same:         {sum(1 for _, l in good if l)}",
            f"  different:    {sum(1 for _, l in good if not l)}",
        ]
        if t is not None:
            lines.append(f"  best threshold: {t:.3f}  (classification accuracy {acc:.0%})")
            lines.append(f"  current WALLPAPER_SIM_THRESHOLD = {WALLPAPER_SIM_THRESHOLD}")
            lines.append(f"  >> set WALLPAPER_SIM_THRESHOLD = {t:.3f} in bulk_summary.py")
        else:
            lines.append("  no usable labels - run again with more samples.")
        for ln in lines:
            tk.Label(self.root, text=ln, fg="#d8d8d8", bg="#1e1e1e",
                     font=("Consolas", 12), anchor="w").pack(pady=2)
        tk.Button(self.root, text="QUIT", command=self.root.destroy).pack(pady=10)
        print("\n" + "\n".join(lines))


def _left_screen_x(entries):
    """Returns the screen_x of the leftmost physical screen (min screen_x).

    Uses screen_x, NOT the monitor index: mss enumerates monitors in an order
    that is not stable across restarts, so the index for "left" changes between
    boots. screen_x is the physical left coordinate, which is ground truth.
    """
    xs = [e.screen_x for e in entries if e.screen_x is not None]
    if not xs:
        return None
    return min(xs)


def _sample_items(entries, screen_x, n, use_monitor=None):
    rows = [e for e in entries if e.screen_x == screen_x]
    if use_monitor is not None:
        rows = [e for e in rows if e.monitor == use_monitor]
    rows.sort(key=lambda e: e.timestamp)
    seen = set()
    uniq = []
    for e in rows:
        if e.filename in seen:
            continue
        seen.add(e.filename)
        uniq.append(e)
    if len(uniq) <= n:
        picked = uniq
    else:
        idxs = {round(i * (len(uniq) - 1) / (n - 1)) for i in range(n)}
        picked = [uniq[i] for i in sorted(idxs)]
    items = []
    for e in picked:
        path = os.path.join(config.screenshots_path, e.filename)
        if not os.path.exists(path):
            continue
        when = os.path.basename(e.filename)[:12] if os.path.basename(e.filename) else ""
        items.append({
            "path": path,
            "filename": e.filename,
            "monitor": e.monitor,
            "screen_x": e.screen_x,
            "when": when,
        })
    # Show the most-similar screenshots first so the user labels the closest
    # matches (true wallpaper) up front and works down to the clear no-matches.
    items.sort(key=lambda it: wallpaper_similarity(it["path"]), reverse=True)
    return items


def main():
    ap = argparse.ArgumentParser(description="Wallpaper-SIM threshold calibrator")
    ap.add_argument("--samples", type=int, default=200, help="max screenshots to show (default 200)")
    ap.add_argument("--monitor-at", type=int, default=None,
                    help="use a specific screen_x coordinate instead of the leftmost")
    args = ap.parse_args()

    if not os.path.exists(WALLPAPER_REF_PATH):
        print(f"ERROR: wallpaper reference not found at {WALLPAPER_REF_PATH}")
        print("Copy the reference image there first.")
        sys.exit(1)

    create_db()
    entries = get_all_entries()
    screen_x = args.monitor_at if args.monitor_at is not None else _left_screen_x(entries)
    if screen_x is None:
        print("ERROR: no entries with a screen_x coordinate in the database.")
        sys.exit(1)
    items = _sample_items(entries, screen_x, args.samples)
    if not items:
        print(f"ERROR: no screenshot files found for screen_x {screen_x}.")
        sys.exit(1)

    print(f"Reference:      {WALLPAPER_REF_PATH}")
    print(f"Left screen:    screen_x={screen_x}  ({len(items)} screenshot(s) to review)")
    print("Sorted most-similar-first. Click SAME if the screen is the empty wallpaper;")
    print("DIFFERENT for real content.\n")

    root = tk.Tk()
    Calibrator(root, items)
    root.mainloop()


if __name__ == "__main__":
    main()