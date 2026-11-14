"""Checks a prepared backup before it goes anywhere near Finder. Read-only."""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

from wabridge.ios.backup import CHATSTORAGE, WA_GROUP_DOMAIN, file_id, read_mbfile

from .ios_backup_ro import (
    FLAG_FILE,
    BackupReadError,
    blob_path,
    entry,
    extract_chatstorage,
    find_live_backup,
    is_encrypted,
    message_count,
    open_manifest,
)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    warn_only: bool = False

    @property
    def blocking(self) -> bool:
        return not self.ok and not self.warn_only


def _journal_mode(db: Path) -> str:
    with open(db, "rb") as fh:
        header = fh.read(20)
    return "wal" if header[18:20] == b"\x02\x02" else "rollback"


def verify_backup(folder: Path, *, expect_messages: int | None = None) -> list[Check]:
    folder = Path(folder)
    checks: list[Check] = []

    def add(name: str, ok: bool, detail: str = "", warn_only: bool = False) -> None:
        checks.append(Check(name, ok, detail, warn_only))

    encrypted = is_encrypted(folder)
    add("Backup is not encrypted", not encrypted,
        "Turn off 'Encrypt local backup' in Finder and back up again." if encrypted else "")
    if encrypted:
        return checks

    missing_plists = [n for n in ("Info.plist", "Manifest.plist", "Status.plist") if not (folder / n).is_file()]
    add("Info, Manifest and Status plists present", not missing_plists, ", ".join(missing_plists))

    leftovers = [s for s in ("-wal", "-shm") if (folder / f"Manifest.db{s}").exists()]
    add("Manifest.db has no -wal/-shm leftovers", not leftovers,
        ("Finder may reject these: " + ", ".join(leftovers)) if leftovers else "")

    try:
        conn = open_manifest(folder)
    except BackupReadError as exc:
        add("Manifest.db opens", False, str(exc))
        return checks
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        add("Manifest.db integrity", result == "ok", result)

        main = entry(conn, CHATSTORAGE)
        listed = main is not None and main["flags"] == FLAG_FILE
        add("ChatStorage.sqlite is listed", listed)
        if not listed:
            return checks

        stale = [s for s in ("-wal", "-shm") if entry(conn, CHATSTORAGE + s) is not None]
        add("No ChatStorage -wal/-shm entries", not stale, ", ".join(stale))

        chat_blob = blob_path(folder, main["fileID"])
        add("ChatStorage file on disk", chat_blob.is_file(), str(chat_blob))
        if not chat_blob.is_file():
            return checks
        listed_size = read_mbfile(main["file"]).get("Size") if main["file"] else None
        actual_size = chat_blob.stat().st_size
        add("ChatStorage size matches Manifest", listed_size == actual_size,
            f"Manifest says {listed_size}, file is {actual_size}")

        total = missing = 0
        for (file_id,) in conn.execute("SELECT fileID FROM Files WHERE flags=?", (FLAG_FILE,)):
            total += 1
            if not blob_path(folder, file_id).is_file():
                missing += 1
        add("Every listed file has data on disk", missing == 0, f"{missing} of {total} missing", warn_only=True)
    finally:
        conn.close()

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / CHATSTORAGE
        shutil.copyfile(chat_blob, db)
        add("ChatStorage uses a rollback journal, not WAL", _journal_mode(db) == "rollback")
        c = sqlite3.connect(db)
        try:
            result = c.execute("PRAGMA integrity_check").fetchone()[0]
            add("ChatStorage integrity", result == "ok", result)
            n, low, high = c.execute("SELECT COUNT(*), MIN(ZSORT), MAX(ZSORT) FROM ZWAMESSAGE").fetchone()
            add("ZSORT renumbered below zero", n == 0 or (high is not None and high < 0),
                f"{n} messages, ZSORT {low}..{high}")
            if expect_messages is not None:
                add("Message count matches the import", n == expect_messages,
                    f"{n} found, {expect_messages} expected")
        finally:
            c.close()
    return checks


def missing_media(folder: Path) -> tuple[int, int]:
    """(paths in ChatStorage, paths with no file in the backup) for media files and thumbnails."""
    folder = Path(folder)
    with tempfile.TemporaryDirectory() as tmp:
        db = extract_chatstorage(folder, Path(tmp))
        c = sqlite3.connect(db)
        try:
            cols = {r[1] for r in c.execute("PRAGMA table_info(ZWAMEDIAITEM)")}
            paths = []
            for col in ("ZMEDIALOCALPATH", "ZXMPPTHUMBPATH"):
                if col in cols:
                    paths += [r[0] for r in c.execute(
                        f"SELECT {col} FROM ZWAMEDIAITEM WHERE {col} IS NOT NULL AND {col} <> ''")]
        finally:
            c.close()
    conn = open_manifest(folder)
    try:
        missing = 0
        for rel in paths:
            row = entry(conn, "Message/" + rel)
            if row is None or row["flags"] != FLAG_FILE or not blob_path(folder, row["fileID"]).is_file():
                missing += 1
    finally:
        conn.close()
    return len(paths), missing


def verify_work(work_dir: Path) -> list[Check]:
    """Verify work_dir/ios_backup/<UDID> against the pristine copy and the import report."""
    work_dir = Path(work_dir)
    live = find_live_backup(work_dir)
    pristine = work_dir / "ios_backup_pristine" / live.name
    report = work_dir / "convert" / "report.json"
    expect = None
    if pristine.is_dir() and report.is_file():
        with tempfile.TemporaryDirectory() as tmp:
            before = message_count(extract_chatstorage(pristine, Path(tmp)))
        expect = before + json.loads(report.read_text(encoding="utf-8"))["messages_written"]
    checks = verify_backup(live, expect_messages=expect)
    if not any(c.blocking for c in checks):
        total, missing = missing_media(live)
        before = missing_media(pristine)[1] if pristine.is_dir() else 0
        checks.append(Check(
            "Every media path in WhatsApp's database has its file", missing <= before,
            f"{total} paths, {missing} without a file ({before} already missing in the original)",
        ))
    if expect is None:
        checks.append(Check("Pristine copy and report.json present", False,
                            "Cannot confirm the message count.", warn_only=True))
    return checks
