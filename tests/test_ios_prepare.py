import sqlite3
from pathlib import Path

import pytest
from helpers import chat_blob, count_text, make_backup, make_chatstorage, make_msgstore, prepared_work

from wamover.ios_prepare import PrepareError, apply_negative_zsort, assert_not_finder_store, stage_ios


def test_negative_zsort(tmp_path: Path):
    db = tmp_path / "ChatStorage.sqlite"
    make_chatstorage(db)
    assert apply_negative_zsort(db) == 1
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT ZSORT FROM ZWAMESSAGE").fetchone()[0] == -1
    conn.close()


def test_stage_leaves_source_and_pristine_alone(tmp_path: Path):
    original_db = tmp_path / "orig.sqlite"
    make_chatstorage(original_db)
    src = make_backup(tmp_path / "UDID123", original_db)
    msg = tmp_path / "msgstore.db"
    make_msgstore(msg)
    before = (src / "Manifest.db").read_bytes()
    live = stage_ios(msg, src, tmp_path / "work")
    assert (src / "Manifest.db").read_bytes() == before
    pristine = tmp_path / "work" / "ios_backup_pristine" / "UDID123"
    assert count_text(chat_blob(pristine), "hello-from-android") == 0
    assert count_text(chat_blob(live), "hello-from-android") == 1
    conn = sqlite3.connect(chat_blob(live))
    sorts = [r[0] for r in conn.execute("SELECT ZSORT FROM ZWAMESSAGE ORDER BY ZSORT")]
    conn.close()
    assert sorts[0] < 0 and sorts[-1] == -1
    restore_txt = (tmp_path / "work" / "RESTORE.txt").read_text()
    assert "Nothing has been restored" in restore_txt and "check-restore" in restore_txt


def test_stage_refuses_existing_work(tmp_path: Path):
    work, src = prepared_work(tmp_path)
    with pytest.raises(PrepareError, match="already holds a prepared backup"):
        stage_ios(tmp_path / "msgstore.db", src, work)


def test_refuses_finder_store(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("wamover.ios_prepare.FINDER_BACKUP_ROOT", tmp_path)
    with pytest.raises(PrepareError):
        assert_not_finder_store(tmp_path / "Backup" / "UDID")


def test_refuses_encrypted_backup(tmp_path: Path):
    original_db = tmp_path / "orig.sqlite"
    make_chatstorage(original_db)
    src = make_backup(tmp_path / "ENC", original_db, encrypted=True)
    msg = tmp_path / "msgstore.db"
    make_msgstore(msg)
    with pytest.raises(PrepareError, match="encrypted"):
        stage_ios(msg, src, tmp_path / "work")
    assert not (tmp_path / "work" / "ios_backup").exists()
