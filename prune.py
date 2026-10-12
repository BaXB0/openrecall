"""
OpenRecall prune tool (command-line entry point).

Deletes screenshot images + database rows for captured moments that never got
an AI description, once they are older than a week. Days that have not been
run through bulk_summary yet are kept entirely.

Usage:
    python prune.py                    # prune everything older than 7 days
    python prune.py --days 14          # prune everything older than 2 weeks
    python prune.py --dry-run          # show what would be removed
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from openrecall.prune import main as _prune_main  # noqa: E402

if __name__ == "__main__":
    _prune_main()