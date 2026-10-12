"""
Weekly pruning for OpenRecall.

Removes screenshot images (and their database rows) for captured moments that
never got an AI description, once they are older than a week. Only days that
have actually been run through ``bulk_summary`` are touched: if the AI command
has NOT run for a day yet, everything on it is kept so it can still be
summarized later.

"Described" = the timestamp has an entry in the day's ``<day>_vision.json``
cache (i.e. the vision model sampled it, ~once per 15 minutes). After pruning a
day you're left with just the described moments plus the daily ``.md`` summary.

Used automatically by the capture loops (~once per day) and manually via
``prune.bat``.
"""
import argparse
import datetime
import json
import os

from openrecall import config
from openrecall.database import (
    create_db,
    delete_entries_for_timestamp,
    get_all_entries,
    get_entries_for_session,
    get_sessions,
)

DEFAULT_OLDER_THAN_DAYS = 7


def has_ai_run(day: str) -> bool:
    """True if bulk_summary has been executed for this day (even partially)."""
    summaries = config.summaries_path
    if not os.path.isdir(summaries):
        return False
    try:
        names = os.listdir(summaries)
    except OSError:
        return False
    for name in names:
        if name == f"{day}_vision.json":
            return True
        if name.endswith(".md"):
            stem = name[:-3]  # strip .md
            if stem == day or stem.startswith(day + "_"):
                return True
    return False


def described_timestamps(day: str) -> set:
    """The set of moment timestamps the AI already described for a day."""
    path = os.path.join(config.summaries_path, f"{day}_vision.json")
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            return {int(k) for k in json.load(f).keys()}
    except Exception:
        return set()


def prune_old(older_than_days: int = DEFAULT_OLDER_THAN_DAYS, dry_run: bool = False) -> dict:
    """
    Deletes images + rows for undescribed moments of days older than a week.

    Skips any day that has never been run through the AI (no ``.md`` summary and
    no ``_vision.json``), keeping those images intact for a later run.

    Returns a summary dict: {"days_checked": int, "days_pruned": int,
    "moments_removed": int, "files_removed": int, "skipped_unsummarized": int}.
    """
    result = {
        "days_checked": 0,
        "days_pruned": 0,
        "moments_removed": 0,
        "files_removed": 0,
        "skipped_unsummarized": 0,
    }
    cutoff = (datetime.date.today() - datetime.timedelta(days=older_than_days)).isoformat()

    # "Days" are sessions: a session is old when the date of its LAST moment is
    # older than the cutoff (a session running 13 Sep 18:00 -> 14 Sep 06:00 is
    # judged on 14 Sep, so a week-old working night is actually pruned).
    # Pruning works with NO micro-session filter so even a 3-minute day is
    # eventually pruned instead of being invisible forever.
    sessions = get_sessions(min_len_minutes=0)
    sess_end_date = {
        s.key: datetime.datetime.fromtimestamp(s.end).strftime("%Y-%m-%d")
        for s in sessions
    }
    all_days = [s.key for s in sessions]
    old_days = [d for d in all_days if sess_end_date.get(d, d) < cutoff]
    result["days_checked"] = len(all_days)

    # Screenshot files are SHARED: the capture loop reuses the last file of an
    # unchanged monitor, so one file backs many timestamps (and can even span
    # into a kept day). A file must only be deleted when NO remaining row
    # references it -- otherwise pruning a non-described moment would delete an
    # image that a described (AI-kept) moment still points to.
    remove_by_day = {}
    for day in old_days:
        if not has_ai_run(day):
            result["skipped_unsummarized"] += 1
            print(f"  {day}: AI not run yet - keeping everything (run bulk_summary to summarize)")
            continue

        described = described_timestamps(day)
        entries = get_entries_for_session(day, min_len_minutes=0)
        remove_ts = sorted({e.timestamp for e in entries if e.timestamp not in described})

        if remove_ts:
            remove_by_day[day] = remove_ts

    if not remove_by_day:
        return result

    remove_all = {ts for ts_list in remove_by_day.values() for ts in ts_list}
    protected = {
        e.filename for e in get_all_entries()
        if e.timestamp not in remove_all and e.filename
    }

    for day, remove_ts in remove_by_day.items():
        entries = get_entries_for_session(day, min_len_minutes=0)
        result["days_pruned"] += 1
        if dry_run:
            print(f"  {day}: would prune {len(remove_ts)} undescribed moment(s) (dry run)")
            continue

        removed_files = 0
        removed_moments = 0
        for ts in remove_ts:
            for e in entries:
                if e.timestamp != ts or not e.filename:
                    continue
                if e.filename in protected:
                    continue  # still referenced by a kept moment - keep the file
                path = os.path.join(config.screenshots_path, e.filename)
                try:
                    if os.path.exists(path):
                        os.remove(path)
                        removed_files += 1
                except OSError as eo:
                    print(f"      [WARN] could not remove {path}: {eo}")
            removed_moments += 1
            delete_entries_for_timestamp(ts)

        result["moments_removed"] += removed_moments
        result["files_removed"] += removed_files
        print(f"  {day}: pruned {removed_moments} undescribed moment(s), {removed_files} file(s)")

    return result


def prune_if_due(older_than_days: int = DEFAULT_OLDER_THAN_DAYS, force: bool = False) -> bool:
    """Runs prune_old at most once per calendar day (shared stamp file)."""
    if os.environ.get("OPENRECALL_DISABLE_PRUNE", "").strip().lower() in ("1", "true", "yes", "on"):
        print("OpenRecall prune: disabled via OPENRECALL_DISABLE_PRUNE")
        return False
    stamp_file = os.path.join(config.appdata_folder, ".last_prune")
    today = datetime.date.today().isoformat()
    if not force and os.path.exists(stamp_file):
        try:
            with open(stamp_file, "r", encoding="utf-8") as f:
                if f.read().strip() == today:
                    return False
        except OSError:
            pass
    try:
        result = prune_old(older_than_days)
        print(
            f"Auto-prune: {result['moments_removed']} moment(s) / "
            f"{result['files_removed']} file(s) removed "
            f"({result['days_pruned']} day(s) pruned, "
            f"{result['skipped_unsummarized']} not-yet-summarized kept)"
        )
    finally:
        try:
            with open(stamp_file, "w", encoding="utf-8") as f:
                f.write(today)
        except OSError:
            pass
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenRecall pruning")
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_OLDER_THAN_DAYS,
        help="Delete undescribed moments older than this many days (default 7)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be pruned without deleting anything",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run even if pruning already ran today",
    )
    args = parser.parse_args()

    create_db()
    result = prune_old(args.days, dry_run=args.dry_run)
    print("\nSummary:")
    print(f"  days with data:        {result['days_checked']}")
    print(f"  days pruned:           {result['days_pruned']}")
    print(f"  moments removed:       {result['moments_removed']}")
    print(f"  image files removed:   {result['files_removed']}")
    print(f"  kept (AI not run):     {result['skipped_unsummarized']}")


if __name__ == "__main__":
    main()