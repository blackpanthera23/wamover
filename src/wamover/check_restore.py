"""Did the restore actually land on the iPhone?

The Oct 2026 field report saw a restore report 100% while the phone kept its
old 2 MB ChatStorage. The only reliable check is a fresh backup taken after the
restore: compare its WhatsApp message IDs with the ones wamover imported.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from .ios_backup_ro import BackupReadError, extract_chatstorage, find_live_backup, stanza_ids

LANDED_RATIO = 0.9


@dataclass
class RestoreResult:
    imported: int
    found: int
    phone_db_bytes: int
    prepared_db_bytes: int

    @property
    def ratio(self) -> float:
        return self.found / self.imported if self.imported else 0.0

    @property
    def landed(self) -> bool:
        return self.imported > 0 and self.ratio >= LANDED_RATIO


def check_restore(new_backup: Path, work_dir: Path) -> RestoreResult:
    work_dir = Path(work_dir)
    new_backup = Path(new_backup)
    live = find_live_backup(work_dir)
    pristine = work_dir / "ios_backup_pristine" / live.name
    if new_backup.resolve() in (live.resolve(), pristine.resolve()):
        raise BackupReadError(
            "Pass the NEW backup Finder made after the restore, not wamover's own copies."
        )
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        prepared_db = extract_chatstorage(live, tmp / "prepared")
        before_db = extract_chatstorage(pristine, tmp / "pristine")
        phone_db = extract_chatstorage(new_backup, tmp / "phone")
        imported = stanza_ids(prepared_db) - stanza_ids(before_db)
        found = imported & stanza_ids(phone_db)
        return RestoreResult(
            imported=len(imported),
            found=len(found),
            phone_db_bytes=phone_db.stat().st_size,
            prepared_db_bytes=prepared_db.stat().st_size,
        )
