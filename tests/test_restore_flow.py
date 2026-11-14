"""verify, install-backup and check-restore against synthetic backups."""

import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from helpers import chat_blob, make_backup, make_chatstorage, prepared_work

from wamover.check_restore import check_restore
from wamover.cli import main
from wamover.install import InstallError, apply_install, plan_install
from wamover.ios_backup_ro import find_live_backup
from wamover.verify import verify_backup, verify_work


def _snapshot(folder: Path) -> dict[str, bytes]:
    return {str(p.relative_to(folder)): p.read_bytes() for p in folder.rglob("*") if p.is_file()}


def test_verify_passes_and_is_read_only(tmp_path):
    work, _ = prepared_work(tmp_path)
    live = find_live_backup(work)
    before = _snapshot(live)
    checks = verify_work(work)
    assert not [c for c in checks if c.blocking], [c for c in checks if c.blocking]
    assert any(c.name == "Message count matches the import" and c.ok for c in checks)
    assert _snapshot(live) == before


def test_verify_catches_positive_zsort(tmp_path):
    db = tmp_path / "orig.sqlite"
    make_chatstorage(db)  # its one message has ZSORT = 1
    folder = make_backup(tmp_path / "B", db)
    failed = [c.name for c in verify_backup(folder) if c.blocking]
    assert failed == ["ZSORT renumbered below zero"]


def test_verify_catches_size_mismatch(tmp_path):
    work, _ = prepared_work(tmp_path)
    live = find_live_backup(work)
    with open(chat_blob(live), "ab") as fh:
        fh.write(b"\0" * 10)
    failed = [c.name for c in verify_work(work) if c.blocking]
    assert "ChatStorage size matches Manifest" in failed


def test_install_dry_run_then_apply_keeps_original(tmp_path):
    work, src = prepared_work(tmp_path)
    store = tmp_path / "MobileSync" / "Backup"
    shutil.copytree(src, store / src.name)  # Finder's own backup of the same iPhone
    original = _snapshot(store / src.name)

    assert main(["install-backup", "--work-dir", str(work), "--store", str(store)]) == 0
    assert _snapshot(store / src.name) == original  # dry run touched nothing

    plan = plan_install(work, store)
    assert plan.move_aside is not None
    dest = apply_install(plan, work)
    assert _snapshot(plan.move_aside) == original  # renamed, intact
    assert _snapshot(dest) == _snapshot(find_live_backup(work))
    assert not plan.staging.exists()
    record = json.loads((work / "install.json").read_text())
    assert record["verified"] is True and record["original_moved_to"] == str(plan.move_aside)


def test_install_refuses_unverified_backup(tmp_path):
    work, _ = prepared_work(tmp_path)
    conn = sqlite3.connect(chat_blob(find_live_backup(work)))
    conn.execute("UPDATE ZWAMESSAGE SET ZSORT = 5")
    conn.commit()
    conn.close()
    store = tmp_path / "store"
    with pytest.raises(InstallError, match="failed verification"):
        plan_install(work, store)
    assert not store.exists()


def test_install_refuses_leftover_staging(tmp_path):
    work, src = prepared_work(tmp_path)
    store = tmp_path / "store"
    (store / f".wamover-staging-{src.name}").mkdir(parents=True)
    with pytest.raises(InstallError, match="left over"):
        plan_install(work, store)


def test_install_refuses_dangling_symlink(tmp_path):
    work, _ = prepared_work(tmp_path)
    store = tmp_path / "Backup"
    store.symlink_to(tmp_path / "unmounted-drive")
    with pytest.raises(InstallError, match="not available"):
        plan_install(work, store)


def test_check_restore_landed(tmp_path):
    work, _ = prepared_work(tmp_path)
    phone_after = tmp_path / "after" / "UDID123"
    shutil.copytree(find_live_backup(work), phone_after)
    result = check_restore(phone_after, work)
    assert result.imported == 1 and result.found == 1 and result.landed


def test_check_restore_not_landed(tmp_path):
    work, src = prepared_work(tmp_path)
    phone_after = tmp_path / "after" / "UDID123"
    shutil.copytree(src, phone_after)  # the iPhone kept its old database
    result = check_restore(phone_after, work)
    assert result.imported == 1 and result.found == 0 and not result.landed
    assert main(["check-restore", str(phone_after), "--work-dir", str(work)]) == 1


def test_check_restore_refuses_own_copy(tmp_path):
    work, _ = prepared_work(tmp_path)
    assert main(["check-restore", str(find_live_backup(work)), "--work-dir", str(work)]) == 1
