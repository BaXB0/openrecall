import os
import sys
import argparse


def get_appdata_folder() -> str:
    """Return the default OpenRecall data directory for the current platform.

    Uses each platform's convention — Linux: ``~/.local/share/openrecall``,
    macOS: ``~/Library/Application Support/openrecall``,
    Windows: ``%APPDATA%\\openrecall`` — and creates it if missing.
    Override with the ``OPENRECALL_STORAGE`` environment variable or the
    ``--storage-path`` CLI argument.
    """
    if sys.platform == "win32":
        appdata = os.getenv("APPDATA")
        if not appdata:
            raise EnvironmentError("APPDATA environment variable is not set.")
        path = os.path.join(appdata, "openrecall")
    elif sys.platform == "darwin":
        path = os.path.join(os.path.expanduser("~"), "Library", "Application Support", "openrecall")
    else:
        path = os.path.join(os.path.expanduser("~"), ".local", "share", "openrecall")
    os.makedirs(path, exist_ok=True)
    return path


_DEFAULT_STORAGE = os.environ.get("OPENRECALL_STORAGE") or get_appdata_folder()

parser = argparse.ArgumentParser(description="OpenRecall")

parser.add_argument(
    "--storage-path",
    default=_DEFAULT_STORAGE,
    help="Path to store the screenshots and database",
)

parser.add_argument(
    "--primary-monitor-only",
    action="store_true",
    help="Only record the primary monitor",
    default=False,
)

# parse_known_args so that running under pytest / unittest (which pass their own
# argv) or from any wrapper never crashes on unrecognized flags.
args, _unknown = parser.parse_known_args()


if args.storage_path:
    appdata_folder = args.storage_path
    screenshots_path = os.path.join(appdata_folder, "screenshots")
    db_path = os.path.join(appdata_folder, "recall.db")
else:
    appdata_folder = _DEFAULT_STORAGE
    db_path = os.path.join(appdata_folder, "recall.db")
    screenshots_path = os.path.join(appdata_folder, "screenshots")

# Create directories
for path in (appdata_folder, screenshots_path):
    if not os.path.exists(path):
        try:
            os.makedirs(path, exist_ok=True)
        except Exception:
            pass

summaries_path = os.path.join(appdata_folder, "summaries")
if not os.path.exists(summaries_path):
    try:
        os.makedirs(summaries_path, exist_ok=True)
    except Exception:
        pass

# "Day" boundaries are not calendar days: a session is a run of captured moments
# with no gap between consecutive timestamps larger than SESSION_GAP_HOURS.
# Tune to your sleep pattern (default 2h means "if I was away from the desk for
# 2+ hours, the next moment starts a new day"). Shorter sessions than
# SESSION_MIN_LEN_MINUTES (e.g. a stray midnight notification) are dropped.
SESSION_GAP_HOURS = float(
    os.environ.get("OPENRECALL_SESSION_GAP_HOURS", "2.0")
)
SESSION_MIN_LEN_MINUTES = float(
    os.environ.get("OPENRECALL_SESSION_MIN_LEN_MINUTES", "10")
)

WEB_PORT = 8082