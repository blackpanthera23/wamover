"""Read-only access to an unencrypted Finder backup.

Nothing here writes into a backup folder. Manifest.db is opened with
`mode=ro`, and ChatStorage is copied out to a temporary folder before it is
opened, so even a Finder-owned backup can be inspected safely.
"""

from __future__ import annotations

import plistlib
import shutil
import sqlite3
from pathlib import Path

from wabridge.ios.backup import CHATSTORAGE, WA_GROUP_DOMAIN

FLAG_FILE = 1


class BackupReadError(RuntimeError):
    pass


def open_manifest(folder: Path) -> sqlite3.Connection:
    db = Path(folder) / "Manifest.db"
    if not db.is_file():
        raise BackupReadError(f"No Manifest.db in {folder}. Pass the backup folder itself.")
    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def is_encrypted(folder: Path) -> bool:
    plist = Path(folder) / "Manifest.plist"
    if not plist.is_file():
        return False
    with open(plist, "rb") as fh:
        return bool(plistlib.load(fh).get("IsEncrypted"))


def blob_path(folder: Path, file_id: str) -> Path:
    return Path(folder) / file_id[:2] / file_id


def entry(conn: sqlite3.Connection, relative_path: str, domain: str = WA_GROUP_DOMAIN):
    return conn.execute(
        "SELECT fileID, flags, file FROM Files WHERE domain=? AND relativePath=?",
        (domain, relative_path),
    ).fetchone()


def extract_chatstorage(folder: Path, dest_dir: Path, domain: str = WA_GROUP_DOMAIN) -> Path:
    """Copy ChatStorage.sqlite (and its -wal, if the backup has one) into dest_dir."""
    folder = Path(folder)
    if is_encrypted(folder):
        raise BackupReadError(
            f"{folder} is encrypted. Turn off 'Encrypt local backup' in Finder and back up again."
        )
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    conn = open_manifest(folder)
    try:
        main = entry(conn, CHATSTORAGE, domain)
        if main is None or main["flags"] != FLAG_FILE:
            raise BackupReadError(f"WhatsApp's ChatStorage.sqlite is not in {folder}.")
        # -shm is deliberately skipped: SQLite rebuilds it, and a stale one can mislead it.
        for suffix, row in (("", main), ("-wal", entry(conn, CHATSTORAGE + "-wal", domain))):
            if row is None or row["flags"] != FLAG_FILE:
                continue
            src = blob_path(folder, row["fileID"])
            if not src.is_file():
                raise BackupReadError(f"The file for {CHATSTORAGE}{suffix} is missing: {src}")
            shutil.copyfile(src, dest_dir / f"{CHATSTORAGE}{suffix}")
    finally:
        conn.close()
    return dest_dir / CHATSTORAGE


def stanza_ids(db: Path) -> set[str]:
    """WhatsApp message IDs in a ChatStorage copy. Only call this on a temporary copy."""
    conn = sqlite3.connect(db)
    try:
        return {
            r[0]
            for r in conn.execute("SELECT ZSTANZAID FROM ZWAMESSAGE WHERE ZSTANZAID IS NOT NULL")
        }
    finally:
        conn.close()


def message_count(db: Path) -> int:
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT COUNT(*) FROM ZWAMESSAGE").fetchone()[0]
    finally:
        conn.close()


def find_live_backup(work_dir: Path) -> Path:
    """The single modified backup that `ios-prepare` left in work_dir/ios_backup."""
    root = Path(work_dir) / "ios_backup"
    found = [p for p in sorted(root.iterdir()) if (p / "Manifest.db").is_file()] if root.is_dir() else []
    if not found:
        raise BackupReadError(f"No prepared backup in {root}. Run `wamover ios-prepare` first.")
    if len(found) > 1:
        raise BackupReadError(f"More than one backup in {root}: " + ", ".join(p.name for p in found))
    return found[0]
