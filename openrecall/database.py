import json
import sqlite3
from collections import namedtuple
from contextlib import contextmanager
from datetime import datetime
from typing import Any, List, Optional, Tuple

import numpy as np

from openrecall.config import db_path
from openrecall.sessions import Session, session_by_key, split_sessions

# Define the structure of a database entry using namedtuple
Entry = namedtuple(
    "Entry",
    [
        "id",
        "app",
        "title",
        "text",
        "timestamp",
        "embedding",
        "filename",
        "monitor",
        "screen_x",
        "browser_url",
        "browser_tabs",
        "windows",
    ],
)


def _json_or_none(value):
    """Parses a JSON TEXT column back into a list/dict, or None."""
    if not value:
        return None
    try:
        return json.loads(value)
    except Exception:
        return None


def _jsonify(value):
    """Serialises a list/dict to JSON text for storage (passes strings through)."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return None


@contextmanager
def _connect():
    """Yields an open DB connection and always closes it afterwards.

    ``with sqlite3.connect(...)`` only manages transactions -- it does NOT
    close the connection, which leaves the file locked on Windows until GC.
    """
    conn = sqlite3.connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


def create_db() -> None:
    """
    Creates the SQLite database and the 'entries' table if they don't exist.

    The table schema includes columns for an auto-incrementing ID, application name,
    window title, extracted text, timestamp, and text embedding. Optional columns
    for the screenshot filename and monitor index are added (and migrated in via
    ALTER TABLE for older databases).
    """
    try:
        with _connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """CREATE TABLE IF NOT EXISTS entries (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       app TEXT,
                       title TEXT,
                       text TEXT,
                       timestamp INTEGER,
                       embedding BLOB,
                       filename TEXT,
                       monitor INTEGER,
                       screen_x INTEGER,
                       browser_url TEXT,
                       browser_tabs TEXT,
                       windows TEXT,
                       UNIQUE(timestamp, monitor)
                   )"""
            )
            # Add index on timestamp for faster lookups
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_timestamp ON entries (timestamp)"
            )
            # Migrate older databases that lack the new columns
            columns = [col[1] for col in cursor.execute("PRAGMA table_info(entries)").fetchall()]
            if "filename" not in columns:
                cursor.execute("ALTER TABLE entries ADD COLUMN filename TEXT")
            if "monitor" not in columns:
                cursor.execute("ALTER TABLE entries ADD COLUMN monitor INTEGER")
            if "screen_x" not in columns:
                cursor.execute("ALTER TABLE entries ADD COLUMN screen_x INTEGER")
            if "browser_url" not in columns:
                cursor.execute("ALTER TABLE entries ADD COLUMN browser_url TEXT")
            if "browser_tabs" not in columns:
                cursor.execute("ALTER TABLE entries ADD COLUMN browser_tabs TEXT")
            if "windows" not in columns:
                cursor.execute("ALTER TABLE entries ADD COLUMN windows TEXT")
            conn.commit()
    except sqlite3.Error as e:
        print(f"Database error during table creation: {e}")


def _row_to_entry(row) -> Entry:
    return Entry(
        id=row["id"],
        app=row["app"],
        title=row["title"],
        text=row["text"],
        timestamp=row["timestamp"],
        embedding=np.frombuffer(row["embedding"], dtype=np.float32),
        filename=row["filename"],
        monitor=row["monitor"],
        screen_x=row["screen_x"],
        browser_url=row["browser_url"],
        browser_tabs=_json_or_none(row["browser_tabs"]),
        windows=_json_or_none(row["windows"]),
    )


_ALL_COLUMNS = (
    "id, app, title, text, timestamp, embedding, filename, monitor, screen_x, "
    "browser_url, browser_tabs, windows"
)


def get_all_entries() -> List[Entry]:
    """
    Retrieves all entries from the database.

    Returns:
        List[Entry]: A list of all entries as Entry namedtuples.
                     Returns an empty list if the table is empty or an error occurs.
    """
    entries: List[Entry] = []
    try:
        with _connect() as conn:
            conn.row_factory = sqlite3.Row  # Return rows as dictionary-like objects
            cursor = conn.cursor()
            cursor.execute(
                f"SELECT {_ALL_COLUMNS} FROM entries ORDER BY timestamp DESC"
            )
            for row in cursor.fetchall():
                # Deserialize the embedding blob back into a NumPy array
                entries.append(_row_to_entry(row))
    except sqlite3.Error as e:
        print(f"Database error while fetching all entries: {e}")
    return entries


def get_timestamps() -> List[int]:
    """
    Retrieves all timestamps from the database, ordered descending.

    Returns:
        List[int]: A list of all timestamps.
                   Returns an empty list if the table is empty or an error occurs.
    """
    timestamps: List[int] = []
    try:
        with _connect() as conn:
            cursor = conn.cursor()
            # Use the index for potentially faster retrieval
            cursor.execute("SELECT timestamp FROM entries ORDER BY timestamp DESC")
            results = cursor.fetchall()
            timestamps = [result[0] for result in results]
    except sqlite3.Error as e:
        print(f"Database error while fetching timestamps: {e}")
    return timestamps


def get_entries_for_day(day_iso: str) -> List[Entry]:
    """
    Retrieves all entries that fall within a given UTC day (YYYY-MM-DD).

    Args:
        day_iso (str): A date string in YYYY-MM-DD format.

    Returns:
        List[Entry]: Entries captured during that day, ordered by time ascending.
    """
    entries: List[Entry] = []
    try:
        entries = get_entries_between(
            int(datetime.strptime(day_iso, "%Y-%m-%d").timestamp()),
            int(datetime.strptime(day_iso, "%Y-%m-%d").timestamp()) + 86400,
        )
    except (sqlite3.Error, ValueError) as e:
        print(f"Database error while fetching entries for day: {e}")
    return entries


def get_entries_between(start_ts: int, end_ts: int, inclusive_end: bool = False) -> List[Entry]:
    """
    Retrieves all entries with ``start_ts <= timestamp < end_ts`` (or
    ``<= end_ts`` when ``inclusive_end``), ascending.

    Shared primitive behind the legacy calendar-day fetch (exclusive upper
    bound = next midnight) and the session fetch (both bounds are real captured
    timestamps, so the last moment must be included).
    """
    entries: List[Entry] = []
    try:
        with _connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            op = "<=" if inclusive_end else "<"
            cursor.execute(
                f"SELECT {_ALL_COLUMNS} "
                f"FROM entries WHERE timestamp >= ? AND timestamp {op} ? ORDER BY timestamp ASC",
                (int(start_ts), int(end_ts)),
            )
            for row in cursor.fetchall():
                entries.append(_row_to_entry(row))
    except sqlite3.Error as e:
        print(f"Database error while fetching entries: {e}")
    return entries


def get_sessions(
    gap_hours: Optional[float] = None,
    min_len_minutes: Optional[float] = None,
) -> List[Session]:
    """
    Computes the session list (a "day" = a run of moments with no gap longer
    than ``gap_hours``) from every recorded timestamp, oldest first.

    Sessions are keyed by the LOCAL date they started on; a second session
    starting on the same date gets a ``_2`` suffix. ``min_len_minutes``
    controls the micro-session filter (UI default 10 min; pass 0 when pruning
    so even a 3-minute day is still pruned eventually).
    """
    return split_sessions(get_timestamps(), gap_hours=gap_hours, min_len_minutes=min_len_minutes)


def get_entries_for_session(
    key: str,
    gap_hours: Optional[float] = None,
    min_len_minutes: Optional[float] = None,
) -> List[Entry]:
    """
    Retrieves all entries belonging to one session key (e.g. "2026-09-13" or
    "2026-09-13_2"), ordered by time ascending. Empty list if no such session.
    """
    session = session_by_key(get_sessions(gap_hours, min_len_minutes), key)
    if not session:
        return []
    return get_entries_between(session.start, session.end, inclusive_end=True)


def get_days_with_entries() -> List[str]:
    """
    Retrieves all distinct local dates (YYYY-MM-DD) that have at least one entry.

    Uses the same local-day boundaries as ``get_entries_for_day`` so the set of
    days returned here matches what ``get_entries_for_day`` can fetch.

    Returns:
        List[str]: Sorted list of day strings (ascending).
    """
    days = set()
    try:
        with _connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT timestamp FROM entries")
            for row in cursor.fetchall():
                days.add(datetime.fromtimestamp(row["timestamp"]).strftime("%Y-%m-%d"))
    except (sqlite3.Error, ValueError, OSError) as e:
        print(f"Database error while fetching days: {e}")
    return sorted(days)


def delete_entries_for_timestamp(timestamp: int) -> int:
    """
    Deletes every row (all monitors) recorded for a given timestamp.

    Args:
        timestamp (int): The shared Unix timestamp of a captured moment.

    Returns:
        int: The number of rows deleted (0 if nothing matched).
    """
    try:
        with _connect() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM entries WHERE timestamp = ?", (int(timestamp),))
            conn.commit()
            return cursor.rowcount
    except sqlite3.Error as e:
        print(f"Database error while deleting timestamp {timestamp}: {e}")
        return 0


def insert_entry(
    text: str,
    timestamp: int,
    embedding: np.ndarray,
    app: str,
    title: str,
    filename: Optional[str] = None,
    monitor: Optional[int] = None,
    screen_x: Optional[int] = None,
    browser_url: Optional[str] = None,
    browser_tabs: Optional[list] = None,
    windows: Optional[list] = None,
) -> Optional[int]:
    """
    Inserts a new entry into the database.

    Args:
        text (str): The extracted text content.
        timestamp (int): The Unix timestamp of the screenshot.
        embedding (np.ndarray): The embedding vector for the text.
        app (str): The name of the active application.
        title (str): The title of the active window.
        filename (str, optional): The name of the saved screenshot file.
        monitor (int, optional): The zero-based monitor index this was captured from.
        screen_x (int, optional): The monitor's physical left coordinate (used to order
                                  screens left-to-right in the web view).
        browser_url (str, optional): The active tab URL at capture time, if a
                                     browser window was on screen.
        browser_tabs (list, optional): JSON-serialisable list of the active
                                       tabs of every visible browser window.
        windows (list, optional): JSON-serialisable list of visible top-level
                                  window titles at capture time.

    Returns:
        Optional[int]: The ID of the newly inserted row, or None if insertion fails.
                       Prints an error message to stderr on failure.
    """
    embedding_bytes: bytes = embedding.astype(np.float32).tobytes() # Ensure consistent dtype
    last_row_id: Optional[int] = None
    try:
        with _connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """INSERT INTO entries (text, timestamp, embedding, app, title, filename, monitor, screen_x, browser_url, browser_tabs, windows)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(timestamp, monitor) DO NOTHING""", # one row per monitor per timestamp
                (text, timestamp, embedding_bytes, app, title, filename, monitor, screen_x, browser_url,
                 _jsonify(browser_tabs), _jsonify(windows)),
            )
            conn.commit()
            if cursor.rowcount > 0: # Check if insert actually happened
                last_row_id = cursor.lastrowid
            # else:
                # Optionally log that a duplicate timestamp was encountered
                # print(f"Skipped inserting entry with duplicate timestamp: {timestamp}")

    except sqlite3.Error as e:
        # More specific error handling can be added (e.g., IntegrityError for UNIQUE constraint)
        print(f"Database error during insertion: {e}")
    return last_row_id
