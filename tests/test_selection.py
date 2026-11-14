"""Subset migration: --since, --chats, the plan CSV, and running stages one after another."""

import csv
import platform
import shutil
import sqlite3
from pathlib import Path

import pytest
from helpers import chat_blob, make_backup, make_chatstorage

from wamover.android_reader import load_archive
from wamover.cli import main
from wamover.ios_prepare import PrepareError, clone_tree, stage_ios
from wamover.selection import (
    Selection,
    SelectionError,
    apply_selection,
    parse_since,
    read_chat_list,
    write_plan,
)

PHONE = "60123456789@s.whatsapp.net"
GROUP = "111-222@g.us"
CUT = 1700000100000  # between k2 (…060000) and k3 (…120000)


def _stanzas(db: Path) -> list[str]:
    conn = sqlite3.connect(db)
    try:
        return [r[0] for r in conn.execute("SELECT ZSTANZAID FROM ZWAMESSAGE WHERE ZSTANZAID IS NOT NULL")]
    finally:
        conn.close()


def test_since_filter_keeps_newer_messages_only(android_db):
    archive = load_archive(android_db)
    before = sum(len(c.messages) for c in archive.chats)
    picked = apply_selection(archive, Selection(since_ms=CUT))
    keys = sorted(m.key_id for c in picked.chats for m in c.messages)
    assert keys == ["k3", "k4"]
    assert sum(len(c.messages) for c in archive.chats) == before  # input untouched


def test_chat_filter(android_db):
    picked = apply_selection(load_archive(android_db), Selection(jids={GROUP}))
    assert [c.jid for c in picked.chats] == [GROUP]


def test_parse_since():
    assert parse_since(None) is None
    assert parse_since("2026-01-01") > parse_since("2025-12-31")
    with pytest.raises(SelectionError):
        parse_since("01/01/2026")


def test_plan_csv_roundtrip(android_db, tmp_path):
    out = tmp_path / "chats.csv"
    totals = write_plan(load_archive(android_db), out, since_ms=CUT)
    assert totals["chats_total"] == 2 and totals["chats_selected"] == 2
    with open(out, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["messages_in_range"] == "1"
    for row in rows:
        row["include"] = "y" if row["jid"] == PHONE else ""
    with open(out, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    assert read_chat_list(out) == {PHONE}
    with pytest.raises(SelectionError, match="already exists"):
        write_plan(load_archive(android_db), out, since_ms=None)


def test_chat_list_needs_a_yes(tmp_path):
    p = tmp_path / "none.csv"
    p.write_text("include,jid\n,a@s.whatsapp.net\n", encoding="utf-8")
    with pytest.raises(SelectionError, match="No chat"):
        read_chat_list(p)


def test_plan_cli(android_db, tmp_path):
    out = tmp_path / "plan.csv"
    assert main(["plan", str(android_db), "--since", "2023-01-01", "--out", str(out)]) == 0
    assert out.is_file()


@pytest.mark.skipif(platform.system() != "Darwin", reason="APFS clone is macOS only")
def test_clone_tree_uses_apfs_clone_and_never_overwrites(tmp_path):
    src = tmp_path / "src"
    (src / "ab").mkdir(parents=True)
    (src / "ab" / "f").write_bytes(b"data")
    assert clone_tree(src, tmp_path / "dst") == "clone"
    (tmp_path / "dst" / "ab" / "f").write_bytes(b"changed")
    assert (src / "ab" / "f").read_bytes() == b"data"
    with pytest.raises(PrepareError, match="already exists"):
        clone_tree(src, tmp_path / "dst")


def test_two_stages_add_up_without_duplicates(android_db, tmp_path):
    orig_db = tmp_path / "orig.sqlite"
    make_chatstorage(orig_db)
    phone_backup = make_backup(tmp_path / "finder" / "UDID9", orig_db)
    finder_before = (phone_backup / "Manifest.db").read_bytes()

    # Stage 1: only the newest messages of one chat.
    live1 = stage_ios(android_db, phone_backup, tmp_path / "stage1",
                      selection=Selection(since_ms=CUT, jids={PHONE}), out=lambda _l: None)
    assert sorted(set(_stanzas(chat_blob(live1))) - {"seed-1"}) == ["k4"]
    assert (phone_backup / "Manifest.db").read_bytes() == finder_before

    # Pretend that backup was restored and the iPhone was backed up again.
    after = tmp_path / "after" / "UDID9"
    shutil.copytree(live1, after)

    # Stage 2: everything for that chat. Only the older messages are new.
    live2 = stage_ios(android_db, after, tmp_path / "stage2",
                      selection=Selection(jids={PHONE}), out=lambda _l: None)
    ids = _stanzas(chat_blob(live2))
    assert sorted(set(ids) - {"seed-1"}) == ["k1", "k2", "k4"]
    assert len(ids) == len(set(ids))  # no duplicates
    import json

    stage2 = json.loads((tmp_path / "stage2" / "stage.json").read_text())
    assert stage2["messages_written"] == 2 and stage2["messages_already_on_iphone"] == 1
    conn = sqlite3.connect(chat_blob(live2))
    assert conn.execute("SELECT MAX(ZSORT) FROM ZWAMESSAGE").fetchone()[0] == -1
    conn.close()


def test_empty_selection_stops_before_copying(android_db, tmp_path):
    orig_db = tmp_path / "orig.sqlite"
    make_chatstorage(orig_db)
    phone_backup = make_backup(tmp_path / "finder" / "UDID9", orig_db)
    with pytest.raises(PrepareError, match="Nothing to migrate"):
        stage_ios(android_db, phone_backup, tmp_path / "stage1",
                  selection=Selection(since_ms=4102444800000), out=lambda _l: None)  # year 2100
    assert not (tmp_path / "stage1" / "ios_backup").exists()
