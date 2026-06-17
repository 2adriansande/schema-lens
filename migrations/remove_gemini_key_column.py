"""
Migration: remove_gemini_key_column
─────────────────────────────────────────────────────────────────────────
Purpose
    Removes the legacy `gemini_key` column from the `users` table.

    SchemaLens previously stored a per-user Gemini API key in the
    database. The application now manages the Gemini API key exclusively
    through the server-side GEMINI_API_KEY environment variable, so this
    column is no longer used and may contain leftover API key material
    that should be removed from the database.

    NOTE: app.py already runs this migration automatically on startup
    (see `run_migrations()`). This script exists as a standalone,
    explicit migration for deployments that prefer to run database
    migrations as a separate, auditable step (e.g. in CI/CD or before
    deploying a new release).

Usage
    python migrations/remove_gemini_key_column.py [path/to/schemalens.db]

    If no path is given, defaults to ./schemalens.db relative to the
    project root.

Safety
    - Creates a timestamped backup of the database file before making
      any changes.
    - Uses SQLite's native `ALTER TABLE ... DROP COLUMN`, available in
      SQLite 3.35.0+ (bundled with Python 3.10+ on most platforms).
    - If the column doesn't exist, or DROP COLUMN isn't supported by the
      installed SQLite version, the script exits cleanly without error.
"""

import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


def column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cursor = conn.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cursor.fetchall())


def main():
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "schemalens.db"

    if not db_path.exists():
        print(f"Database not found at {db_path}. Nothing to migrate.")
        return

    conn = sqlite3.connect(str(db_path))
    try:
        if not column_exists(conn, "users", "gemini_key"):
            print("Column 'gemini_key' is already absent from 'users'. Nothing to do.")
            return

        # Backup before any destructive change
        backup_path = db_path.with_suffix(f".pre_gemini_key_removal_{datetime.now(timezone.utc):%Y%m%d%H%M%S}.bak")
        shutil.copy2(db_path, backup_path)
        print(f"Backup created at {backup_path}")

        try:
            conn.execute("ALTER TABLE users DROP COLUMN gemini_key")
            conn.commit()
            print("Removed 'gemini_key' column from 'users' table.")
        except sqlite3.OperationalError as exc:
            print(
                "Could not DROP COLUMN (likely an older SQLite version that "
                f"doesn't support it): {exc}\n"
                "The column will remain in the schema but is no longer read "
                "or written by the application, and contains no new data."
            )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
