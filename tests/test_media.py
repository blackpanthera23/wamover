"""Media and links, modelled on rows WhatsApp itself wrote. macOS only (uses sips/qlmanage)."""

import base64
import hashlib
import json
import platform
import plistlib
import sqlite3
import struct
import subprocess
import zlib
from pathlib import Path

import pytest
from wabridge.ios.backup import CHATSTORAGE, WA_GROUP_DOMAIN, build_mbfile, file_id, read_mbfile

from wamover.android_reader import load_archive
from wamover.ios_backup_ro import open_manifest
from wamover.ios_prepare import stage_ios
from wamover.media import add_links, attach_media, learn
from wamover.verify import verify_work

pytestmark = pytest.mark.skipif(platform.system() != "Darwin", reason="needs macOS sips")

REF_FLAGS = 0x1000189
OUR_OLD_FLAGS = 0x1000000
JID = "60123456789@s.whatsapp.net"


def _png(path: Path, w: int = 400, h: int = 200) -> None:
    rows = b"".join(b"\x00" + b"".join(bytes([(x * 7) % 256, (y * 5) % 256, 90]) for x in range(w)) for y in range(h))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def _jpeg(path: Path) -> None:
    png = path.with_suffix(".png")
    _png(png)
    subprocess.run(["sips", "-s", "format", "jpeg", str(png), "--out", str(path)], check=True, capture_output=True)
    png.unlink()


def make_media_chatstorage(path: Path) -> None:
    """A ChatStorage with one WhatsApp-made downloaded photo and one WhatsApp-made link row."""
    conn = sqlite3.connect(path)
    conn.executescript(f"""
        CREATE TABLE Z_PRIMARYKEY (Z_ENT INTEGER, Z_NAME TEXT, Z_SUPER INTEGER, Z_MAX INTEGER);
        INSERT INTO Z_PRIMARYKEY VALUES (1,'WAChatSession',0,2),(2,'WAMessage',0,3),(3,'WAMediaItem',0,2),
                                        (4,'WAMessageDataItem',0,1);
        CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER,
            ZCONTACTJID TEXT, ZCONTACTIDENTIFIER TEXT, ZPARTNERNAME TEXT, ZSESSIONTYPE INTEGER,
            ZMESSAGECOUNTER INTEGER, ZUNREADCOUNT INTEGER, ZARCHIVED INTEGER, ZHIDDEN INTEGER,
            ZREMOVED INTEGER, ZCONTACTABID INTEGER, ZFLAGS INTEGER, ZSPOTLIGHTSTATUS INTEGER,
            ZLASTMESSAGEDATE REAL, ZLASTMESSAGE INTEGER, ZLASTMESSAGETEXT TEXT,
            ZIDENTITYVERIFICATIONEPOCH INTEGER, ZIDENTITYVERIFICATIONSTATE INTEGER, ZGROUPINFO INTEGER);
        CREATE TABLE ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER,
            ZCHATSESSION INTEGER, ZLASTSESSION INTEGER, ZISFROMME INTEGER, ZMESSAGETYPE INTEGER,
            ZMESSAGESTATUS INTEGER, ZSORT INTEGER, ZMESSAGEDATE REAL, ZSENTDATE REAL, ZTEXT TEXT,
            ZSTANZAID TEXT, ZFROMJID TEXT, ZTOJID TEXT, ZPUSHNAME TEXT, ZFLAGS INTEGER,
            ZSPOTLIGHTSTATUS INTEGER, ZDATAITEMVERSION INTEGER, ZSTARRED INTEGER,
            ZMESSAGEERRORSTATUS INTEGER, ZDOCID INTEGER, ZCHILDMESSAGESDELIVEREDCOUNT INTEGER,
            ZCHILDMESSAGESPLAYEDCOUNT INTEGER, ZCHILDMESSAGESREADCOUNT INTEGER,
            ZFILTEREDRECIPIENTCOUNT INTEGER, ZENCRETRYCOUNT INTEGER, ZGROUPEVENTTYPE INTEGER,
            ZMEDIAITEM INTEGER, ZMEDIASECTIONID TEXT, ZGROUPMEMBER INTEGER);
        CREATE TABLE ZWAMEDIAITEM (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER,
            ZCLOUDSTATUS INTEGER, ZFILESIZE INTEGER, ZMEDIAORIGIN INTEGER, ZMOVIEDURATION INTEGER,
            ZMESSAGE INTEGER, ZASPECTRATIO REAL, ZHACCURACY REAL, ZLATITUDE REAL, ZLONGITUDE REAL,
            ZMEDIAURLDATE REAL, ZAUTHORNAME TEXT, ZCOLLECTIONNAME TEXT, ZMEDIALOCALPATH TEXT,
            ZMEDIAURL TEXT, ZTHUMBNAILLOCALPATH TEXT, ZTITLE TEXT, ZVCARDNAME TEXT, ZVCARDSTRING TEXT,
            ZXMPPTHUMBPATH TEXT, ZMEDIAKEY BLOB, ZMETADATA BLOB);
        CREATE TABLE ZWAMESSAGEDATAITEM (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER,
            ZINDEX INTEGER, ZOWNSTHUMBNAIL INTEGER, ZTYPE INTEGER, ZMESSAGE INTEGER, ZDATE REAL,
            ZCHATJID TEXT, ZCONTENT1 TEXT, ZCONTENT2 TEXT, ZMATCHEDTEXT TEXT, ZSECTIONID TEXT,
            ZSENDERJID TEXT, ZSUMMARY TEXT, ZTHUMBNAILPATH TEXT, ZTITLE TEXT);
        INSERT INTO ZWACHATSESSION (Z_PK,Z_ENT,Z_OPT,ZCONTACTJID,ZCONTACTIDENTIFIER,ZPARTNERNAME,ZSESSIONTYPE,
            ZFLAGS,ZSPOTLIGHTSTATUS,ZMESSAGECOUNTER) VALUES
            (1,1,1,'111@lid','60111111111@s.whatsapp.net','Ref',0,272,-5,2),
            (2,1,1,'999@lid','{JID}','Ali',0,272,-5,0);
        INSERT INTO ZWAMESSAGE (Z_PK,Z_ENT,Z_OPT,ZCHATSESSION,ZISFROMME,ZMESSAGETYPE,ZMESSAGESTATUS,ZSORT,
            ZMESSAGEDATE,ZTEXT,ZSTANZAID,ZFLAGS,ZMEDIAITEM) VALUES
            (1,2,1,1,0,1,6,1,816000000,NULL,'wa-photo',{REF_FLAGS},1),
            (2,2,1,1,0,0,6,2,816000100,'see https://example.org/x','wa-link',16777216,2);
        INSERT INTO ZWAMEDIAITEM (Z_PK,Z_ENT,Z_OPT,ZMESSAGE,ZFILESIZE,ZLATITUDE,ZLONGITUDE,ZMEDIALOCALPATH,
            ZXMPPTHUMBPATH,ZVCARDNAME,ZVCARDSTRING) VALUES
            (1,3,1,1,10,1080,607,'Media/111@lid/a/b/ab.jpg','Media/111@lid/a/b/ab.thumb','hash=','image/jpeg'),
            (2,3,1,2,NULL,NULL,NULL,NULL,NULL,NULL,NULL);
        INSERT INTO ZWAMESSAGEDATAITEM VALUES (1,4,1,0,0,0,2,816000100,'p1','https://example.org/x',NULL,
            'https://example.org/x','2026-11',NULL,NULL,NULL,NULL);
    """)
    conn.commit()
    conn.close()


def make_msgstore(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(f"""
        CREATE TABLE jid (_id INTEGER PRIMARY KEY, raw_string TEXT, user TEXT, server TEXT);
        CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
        CREATE TABLE message (_id INTEGER PRIMARY KEY, chat_row_id INTEGER, from_me INTEGER,
            sender_jid_row_id INTEGER, timestamp INTEGER, text_data TEXT, message_type INTEGER, key_id TEXT);
        CREATE TABLE message_media (message_row_id INTEGER, file_path TEXT, mime_type TEXT, file_size INTEGER,
            media_name TEXT, media_duration INTEGER, width INTEGER, height INTEGER, media_caption TEXT);
        INSERT INTO jid VALUES (1,'{JID}','60123456789','s.whatsapp.net');
        INSERT INTO chat VALUES (1,1,NULL);
        INSERT INTO message VALUES (1,1,0,1,1700000000000,NULL,1,'img-1');
        INSERT INTO message VALUES (2,1,0,1,1700000060000,NULL,3,'vid-1');
        INSERT INTO message VALUES (3,1,1,NULL,1700000120000,'look www.example.com/a, and https://x.io/b.',0,'txt-1');
        INSERT INTO message VALUES (4,1,0,1,1700000180000,NULL,1,'img-gone');
        INSERT INTO message_media VALUES (1,'Media/WhatsApp Images/IMG-1.jpg','image/jpeg',0,NULL,NULL,400,200,'a cap');
        INSERT INTO message_media VALUES (2,'Media/WhatsApp Video/VID-1.mp4','video/mp4',0,NULL,5,640,360,NULL);
        INSERT INTO message_media VALUES (4,'Media/WhatsApp Images/IMG-deleted.jpg','image/jpeg',0,NULL,NULL,1,1,NULL);
    """)
    conn.commit()
    conn.close()


def make_backup(folder: Path, chat_db: Path) -> Path:
    folder.mkdir(parents=True)
    conn = sqlite3.connect(folder / "Manifest.db")
    conn.execute("CREATE TABLE Files (fileID TEXT PRIMARY KEY, domain TEXT, relativePath TEXT, flags INTEGER, file BLOB)")
    entries = [(CHATSTORAGE, chat_db.read_bytes(), 1), ("Message", b"", 2), ("Message/Media", b"", 2),
               ("Message/Media/111@lid/a/b/ab.jpg", b"real", 1), ("Message/Media/111@lid/a/b/ab.thumb", b"t", 1)]
    for rel, data, flags in entries:
        fid = file_id(WA_GROUP_DOMAIN, rel)
        if flags == 1:
            (folder / fid[:2]).mkdir(exist_ok=True)
            (folder / fid[:2] / fid).write_bytes(data)
        mode = 0o100644 if flags == 1 else 0o40755
        conn.execute("INSERT INTO Files VALUES (?,?,?,?,?)",
                     (fid, WA_GROUP_DOMAIN, rel, flags, build_mbfile(rel, len(data), mode, 1, 1,
                                                                       protection_class=3 if flags == 1 else 0)))
    conn.commit()
    conn.close()
    for name, data in (("Manifest.plist", {"IsEncrypted": False}), ("Info.plist", {"Device Name": "T"}),
                       ("Status.plist", {"BackupState": "new"})):
        (folder / name).write_bytes(plistlib.dumps(data))
    return folder


@pytest.fixture
def world(tmp_path):
    chat_db = tmp_path / "ChatStorage.sqlite"
    make_media_chatstorage(chat_db)
    msg = tmp_path / "msgstore.db"
    make_msgstore(msg)
    media = tmp_path / "Media"
    (media / "WhatsApp Images").mkdir(parents=True)
    (media / "WhatsApp Video").mkdir(parents=True)
    _jpeg(media / "WhatsApp Images" / "IMG-1.jpg")
    (media / "WhatsApp Video" / "VID-1.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    return chat_db, msg, media


def test_learning_ignores_rows_from_earlier_stages(world):
    chat_db, _, _ = world
    conn = sqlite3.connect(chat_db)
    flags, profiles = learn(conn, ours=set())
    assert flags[(1, False, False)] == REF_FLAGS and profiles[1].uses_thumb and profiles[1].uses_hash
    flags, profiles = learn(conn, ours={"wa-photo"})
    assert flags == {} and profiles == {}
    conn.close()


def test_attach_copies_whatsapps_conventions(world, tmp_path):
    from wabridge.ios.chatstorage import Writer

    chat_db, msg, media = world
    archive = load_archive(msg)
    writer = Writer(str(chat_db))
    writer.import_archive(archive, media_root=None, include_media=False)
    writer.finalize()

    report = attach_media(chat_db, archive, media, tmp_path / "thumbs", out=lambda _l: None)
    assert report.attached == {1: 1}
    assert report.no_reference == {(2, False, False): 1}   # no WhatsApp-made video on the "iPhone"
    assert report.no_file_on_android == {1: 1}    # img-gone
    assert report.thumbs == 1

    conn = sqlite3.connect(chat_db)
    local, thumb, sha, mime, size, w, h, title, flags = conn.execute(
        "SELECT i.ZMEDIALOCALPATH, i.ZXMPPTHUMBPATH, i.ZVCARDNAME, i.ZVCARDSTRING, i.ZFILESIZE, "
        "i.ZLONGITUDE, i.ZLATITUDE, i.ZTITLE, m.ZFLAGS FROM ZWAMESSAGE m JOIN ZWAMEDIAITEM i ON i.Z_PK=m.ZMEDIAITEM "
        "WHERE m.ZSTANZAID='img-1'").fetchone()
    conn.close()
    src = media / "WhatsApp Images" / "IMG-1.jpg"
    assert local.startswith("Media/999@lid/") and local.endswith(".jpg")
    u = local.rsplit("/", 1)[1]
    assert local.split("/")[2:4] == [u[0], u[1]]
    assert thumb == local[:-4] + ".thumb"
    assert sha == base64.b64encode(hashlib.sha256(src.read_bytes()).digest()).decode()
    assert (mime, size, (w, h), title, flags) == ("image/jpeg", src.stat().st_size, (400.0, 200.0), "a cap", REF_FLAGS)
    tfile = tmp_path / "thumbs" / thumb
    assert tfile.read_bytes()[:2] == b"\xff\xd8"
    dims = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(tfile)],
                          capture_output=True, text=True).stdout.split()
    assert max(int(dims[-3]), int(dims[-1])) <= 96
    rels = sorted(j["relative_path"] for j in report.jobs)
    assert rels == sorted(["Message/" + local, "Message/" + thumb])
    assert src.read_bytes()[:2] == b"\xff\xd8"  # source untouched


def test_links_copy_whatsapps_row_shape(world):
    from wabridge.ios.chatstorage import Writer

    chat_db, msg, _ = world
    archive = load_archive(msg)
    writer = Writer(str(chat_db))
    writer.import_archive(archive, media_root=None, include_media=False)
    writer.finalize()
    assert add_links(chat_db, archive) == 2
    assert add_links(chat_db, archive) == 0  # idempotent
    conn = sqlite3.connect(chat_db)
    rows = conn.execute(
        "SELECT d.ZINDEX, d.ZTYPE, d.ZCHATJID, d.ZCONTENT1, d.ZMATCHEDTEXT, d.ZSECTIONID, m.ZCHATSESSION "
        "FROM ZWAMESSAGEDATAITEM d JOIN ZWAMESSAGE m ON m.Z_PK=d.ZMESSAGE WHERE m.ZSTANZAID='txt-1' ORDER BY 1"
    ).fetchall()
    zmax = conn.execute("SELECT Z_MAX FROM Z_PRIMARYKEY WHERE Z_NAME='WAMessageDataItem'").fetchone()[0]
    top = conn.execute("SELECT MAX(Z_PK) FROM ZWAMESSAGEDATAITEM").fetchone()[0]
    conn.close()
    assert [r[3] for r in rows] == ["http://www.example.com/a", "https://x.io/b"]
    assert [r[4] for r in rows] == ["www.example.com/a", "https://x.io/b"]
    assert all(r[1] == 0 and r[2] == f"p{r[6]}" and r[5] == "2023-11" for r in rows)
    assert [r[0] for r in rows] == [0, 1] and zmax == top


def test_stage_with_media_puts_files_where_whatsapp_looks(world, tmp_path):
    chat_db, msg, media = world
    src = make_backup(tmp_path / "finder" / "UDIDM", chat_db)
    before = (src / "Manifest.db").read_bytes()
    live = stage_ios(msg, src, tmp_path / "stage", out=lambda _l: None, media_root=media)
    assert (src / "Manifest.db").read_bytes() == before

    stage = json.loads((tmp_path / "stage" / "stage.json").read_text())
    assert stage["media"]["attached"] == {"photo": 1} and stage["links_added"] == 2

    checks = verify_work(tmp_path / "stage")
    assert not [c for c in checks if c.blocking], [c for c in checks if c.blocking]
    assert any(c.name.startswith("Every media path") and c.ok for c in checks)

    conn = open_manifest(live)
    media_rows = conn.execute(
        "SELECT relativePath, flags, file FROM Files WHERE relativePath LIKE 'Message/Media/999@lid%'").fetchall()
    conn.close()
    files = [r for r in media_rows if r[1] == 1]
    dirs = [r for r in media_rows if r[1] == 2]
    assert {Path(r[0]).suffix for r in files} == {".jpg", ".thumb"}
    assert dirs and all(read_mbfile(r[2])["Mode"] == 0o40755 for r in dirs)
    assert all(read_mbfile(r[2])["Mode"] == 0o100644 for r in files)
    import shutil as real_shutil

    import wabridge.ios.backup as b
    assert b.MODE_DIR == 0o40775 and b.shutil is real_shutil  # temporary overrides were undone
    assert stage["copy_method"]["files_into_backup"]["cloned"] >= 2  # photo + thumbnail cloned, not copied


def test_stage_two_attaches_media_to_messages_from_stage_one(world, tmp_path):
    """Stage 1 imported text only; stage 2 must give those same messages their photos."""
    import shutil

    chat_db, msg, media = world
    src = make_backup(tmp_path / "finder" / "UDIDM", chat_db)
    live1 = stage_ios(msg, src, tmp_path / "stage1", out=lambda _l: None)
    after = tmp_path / "after" / "UDIDM"
    shutil.copytree(live1, after)
    stage_ios(msg, after, tmp_path / "stage2", out=lambda _l: None, media_root=media)
    s2 = json.loads((tmp_path / "stage2" / "stage.json").read_text())
    assert s2["messages_written"] == 0 and s2["messages_already_on_iphone"] == 4
    assert s2["media"]["attached"] == {"photo": 1}
    s1 = json.loads((tmp_path / "stage1" / "stage.json").read_text())
    assert s1["links_added"] == 2 and s2["links_added"] == 0  # not added twice
    assert not [c for c in verify_work(tmp_path / "stage2") if c.blocking]


def test_no_borrowing_between_group_and_one_to_one(world):
    from wamover.media import flags_for

    chat_db, _, _ = world
    conn = sqlite3.connect(chat_db)
    flags, _ = learn(conn, ours=set())
    conn.close()
    assert flags_for(flags, 1, False, False) == REF_FLAGS
    assert flags_for(flags, 1, False, True) is None   # group photo: no example, so skipped
    assert flags_for(flags, 1, True, False) is None   # sent photo: no example, so skipped


def test_skip_undownloaded_leaves_out_only_empty_bubbles(world, tmp_path):
    from wamover.media import drop_undownloaded

    chat_db, msg, media = world
    conn = sqlite3.connect(msg)
    conn.execute("INSERT INTO message VALUES (5,1,0,1,1700000240000,NULL,1,'img-gone-cap')")
    conn.execute("INSERT INTO message_media VALUES (5,'Media/WhatsApp Images/IMG-x.jpg','image/jpeg',0,NULL,NULL,1,1,'kept caption')")
    conn.commit()
    conn.close()
    archive = load_archive(msg)
    kept, dropped = drop_undownloaded(archive, media)
    keys = {m.key_id for c in kept.chats for m in c.messages}
    assert dropped == 1 and "img-gone" not in keys
    assert {"img-1", "vid-1", "txt-1", "img-gone-cap"} <= keys
    assert sum(len(c.messages) for c in archive.chats) == 5  # input untouched

    src = make_backup(tmp_path / "finder" / "UDIDS", chat_db)
    stage_ios(msg, src, tmp_path / "stage", out=lambda _l: None, media_root=media, skip_undownloaded=True)
    s = json.loads((tmp_path / "stage" / "stage.json").read_text())
    assert s["undownloaded_left_out"] == 1 and s["messages_written"] == 4


def test_hung_thumbnail_skips_that_file_only(world, tmp_path, monkeypatch):
    """A thumbnail tool that hangs must not stop the run; that one file stays an empty bubble."""
    import wamover.media as media_mod
    from wabridge.ios.chatstorage import Writer

    chat_db, msg, media = world
    archive = load_archive(msg)
    writer = Writer(str(chat_db))
    writer.import_archive(archive, media_root=None, include_media=False)
    writer.finalize()
    real_run = media_mod.subprocess.run

    def hang_on_sips_resize(cmd, *a, **k):
        if cmd[0] == "sips" and "-Z" in cmd:
            raise subprocess.TimeoutExpired(cmd, 1)
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(media_mod.subprocess, "run", hang_on_sips_resize)
    report = attach_media(chat_db, archive, media, tmp_path / "thumbs", out=lambda _l: None)
    assert report.attached == {} and report.no_thumb == {1: 1} and report.jobs == []
    conn = sqlite3.connect(chat_db)
    local = conn.execute("SELECT i.ZMEDIALOCALPATH FROM ZWAMESSAGE m JOIN ZWAMEDIAITEM i ON i.Z_PK=m.ZMEDIAITEM "
                         "WHERE m.ZSTANZAID='img-1'").fetchone()[0]
    conn.close()
    assert local is None
