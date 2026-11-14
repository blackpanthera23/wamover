"""pull (adb faked), contacts, media in the archive, status and doctor."""

import json
from pathlib import Path

import pytest
from wabridge.android import adb

from wamover.android_pull import PullError, pull_android
from wamover.android_reader import read_android_db
from wamover.archive import ArchiveError, MediaResolver, export_html
from wamover.cli import _status, main
from wamover.doctor import run_doctor


@pytest.fixture
def fake_phone(tmp_path, monkeypatch):
    calls = []

    def pull_databases(dest, root, serial=None, progress=None):
        calls.append(("databases", root))
        d = Path(dest) / "Databases"
        d.mkdir(parents=True)
        (d / "msgstore.db.crypt15").write_bytes(b"crypt15-bytes")
        return str(d)

    def pull_media(dest, root, serial=None, progress=None, percent=None):
        calls.append(("media", root))
        (Path(dest) / "Media" / "WhatsApp Images").mkdir(parents=True)
        return ["WhatsApp Images"]

    monkeypatch.setattr(adb, "require_device", lambda serial=None: adb.Device("SER1", "device", "Pixel"))
    monkeypatch.setattr(adb, "detect_wa_root", lambda serial=None, business=False: "/sdcard/WA")
    monkeypatch.setattr(adb, "pull_databases", pull_databases)
    monkeypatch.setattr(adb, "pull_media", pull_media)
    monkeypatch.setattr(adb, "export_contacts", lambda serial=None: {"60123456789": "Ali"})
    return calls


def test_pull_writes_summary_and_contacts(tmp_path, fake_phone):
    summary = pull_android(tmp_path / "android", out=lambda _l: None)
    assert summary["media_dirs"] == ["WhatsApp Images"]
    assert len(summary["crypt15_sha256"]) == 64
    assert json.loads((tmp_path / "android" / "contacts.json").read_text()) == {"60123456789": "Ali"}
    assert [c[0] for c in fake_phone] == ["databases", "media"]


def test_pull_refuses_non_empty_folder(tmp_path, fake_phone):
    dest = tmp_path / "android"
    dest.mkdir()
    (dest / "old.txt").write_text("earlier pull")
    with pytest.raises(PullError, match="not empty"):
        pull_android(dest, out=lambda _l: None)
    assert (dest / "old.txt").read_text() == "earlier pull"
    assert fake_phone == []


def test_pull_reports_adb_errors(tmp_path, monkeypatch):
    def no_phone(serial=None):
        raise adb.AdbError("No Android device found.")

    monkeypatch.setattr(adb, "require_device", no_phone)
    with pytest.raises(PullError, match="No Android device"):
        pull_android(tmp_path / "android", out=lambda _l: None)


def test_contacts_name_the_chat(android_db):
    ex = read_android_db(android_db, {"60123456789": "Ali"})
    assert "Ali" in [c.title for c in ex.chats]


def test_media_is_linked_into_archive(android_db, tmp_path):
    media = tmp_path / "Media" / "WhatsApp Images"
    media.mkdir(parents=True)
    (media / "a.jpg").write_bytes(b"jpeg")
    report = {}
    idx = export_html(read_android_db(android_db), tmp_path / "out", tmp_path / "Media", report)
    assert report == {"media_linked": 1, "media_missing": 0}
    copied = list((idx.parent / "media").iterdir())
    assert len(copied) == 1 and copied[0].read_bytes() == b"jpeg"
    page = "".join(p.read_text() for p in idx.parent.glob("*.html"))
    assert "<img src=\"media/" in page
    assert (media / "a.jpg").read_bytes() == b"jpeg"


def test_media_resolver_stays_inside_root(tmp_path):
    root = tmp_path / "Media"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("x")
    assert MediaResolver(root).resolve("Media/../../secret.txt") is None


def test_export_refuses_to_overwrite(android_db, tmp_path):
    export_html(read_android_db(android_db), tmp_path / "out")
    with pytest.raises(ArchiveError, match="already exists"):
        export_html(read_android_db(android_db), tmp_path / "out")


def test_status_points_at_backup_first(tmp_path):
    lines = _status(tmp_path / "work", tmp_path / "archive")
    text = "\n".join(lines)
    assert "BEFORE registering WhatsApp on the iPhone" in text
    assert "wamover pull" in text


def test_status_after_decrypt(tmp_path, android_db):
    work = tmp_path / "work"
    work.mkdir()
    (work / "msgstore.db").write_bytes(android_db.read_bytes())
    text = "\n".join(_status(work, tmp_path / "archive"))
    assert "wamover export" in text and "ios-prepare" in text


def test_doctor_runs_without_phones(tmp_path, monkeypatch):
    def missing():
        raise adb.AdbError("adb not found")

    monkeypatch.setattr(adb, "find_adb", missing)
    monkeypatch.setattr("wamover.ios_prepare.FINDER_BACKUP_ROOT", tmp_path / "MobileSync")
    checks = run_doctor(tmp_path)
    names = {c.name: c for c in checks}
    assert not names["adb installed"].ok and names["adb installed"].warn_only
    assert names["wabridge engine installed"].ok
    assert main(["doctor", "--work-dir", str(tmp_path)]) == 0
