"""Repair stage: documents and stickers in WhatsApp's own format, found files, archive status."""

import json
import platform
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest
from test_media import JID, _png, make_backup, make_media_chatstorage

from wamover.android_reader import load_archive
from wamover.ios_prepare import stage_ios
from wamover.media import attach_media, carry_archived, learn
from wamover.recover import recovered_for_messages
from wamover.verify import verify_work

pytestmark = pytest.mark.skipif(platform.system() != "Darwin", reason="needs macOS sips/qlmanage")

DOC_FLAGS, STICKER_FLAGS, VOICE_FLAGS = 0x1000000, 0x5000000, 0x1000000


def add_whatsapp_examples(db: Path) -> None:
    """A downloaded document, sticker and voice note, as WhatsApp on the iPhone writes them."""
    conn = sqlite3.connect(db)
    conn.executescript(f"""
        INSERT INTO ZWAMESSAGE (Z_PK,Z_ENT,Z_OPT,ZCHATSESSION,ZISFROMME,ZMESSAGETYPE,ZMESSAGESTATUS,ZSORT,
            ZMESSAGEDATE,ZTEXT,ZSTANZAID,ZFLAGS,ZMEDIAITEM,ZMEDIASECTIONID) VALUES
            (10,2,1,1,0,8,8,10,816000200,'Plan','wa-doc',{DOC_FLAGS},10,'doc-2026-11'),
            (11,2,1,1,0,15,8,11,816000300,NULL,'wa-sticker',{STICKER_FLAGS},11,NULL),
            (12,2,1,1,0,3,8,12,816000400,NULL,'wa-voice',{VOICE_FLAGS},12,NULL);
        INSERT INTO ZWAMEDIAITEM (Z_PK,Z_ENT,Z_OPT,ZMESSAGE,ZFILESIZE,ZLATITUDE,ZLONGITUDE,ZMEDIALOCALPATH,
            ZXMPPTHUMBPATH,ZVCARDNAME,ZVCARDSTRING,ZAUTHORNAME,ZMOVIEDURATION) VALUES
            (10,3,1,10,99,NULL,NULL,'Media/111@lid/c/d/cd.pdf','Media/111@lid/c/d/cd.thumb','h=',
             'application/pdf','Plan.pdf',4),
            (11,3,1,11,50,512,512,'Media/111@lid/e/f/ef.webp',NULL,'h=','image/webp',NULL,NULL),
            (12,3,1,12,70,NULL,NULL,'Media/111@lid/a/a/aa.opus',NULL,'h=','audio/ogg',NULL,9);
        UPDATE Z_PRIMARYKEY SET Z_MAX=12 WHERE Z_NAME IN ('WAMessage','WAMediaItem');
    """)
    conn.commit()
    conn.close()


def make_msgstore(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(f"""
        CREATE TABLE jid (_id INTEGER PRIMARY KEY, raw_string TEXT, user TEXT, server TEXT);
        CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT, archived INTEGER);
        CREATE TABLE message (_id INTEGER PRIMARY KEY, chat_row_id INTEGER, from_me INTEGER,
            sender_jid_row_id INTEGER, timestamp INTEGER, text_data TEXT, message_type INTEGER, key_id TEXT);
        CREATE TABLE message_media (message_row_id INTEGER, file_path TEXT, mime_type TEXT, file_size INTEGER,
            media_name TEXT, media_duration INTEGER, width INTEGER, height INTEGER, media_caption TEXT,
            page_count INTEGER, file_hash TEXT, file_length INTEGER);
        INSERT INTO jid VALUES (1,'{JID}','60123456789','s.whatsapp.net');
        INSERT INTO jid VALUES (2,'60199999999@s.whatsapp.net','60199999999','s.whatsapp.net');
        INSERT INTO chat VALUES (1,1,NULL,1);
        INSERT INTO chat VALUES (2,2,NULL,0);
        INSERT INTO message VALUES (1,1,0,1,1700000000000,'Report.pdf',9,'doc-1');
        INSERT INTO message VALUES (2,1,0,1,1700000060000,NULL,20,'stk-1');
        INSERT INTO message VALUES (3,1,0,1,1700000120000,NULL,9,'doc-found');
        INSERT INTO message VALUES (4,2,0,2,1700000180000,'hi',0,'txt-2');
        INSERT INTO message_media VALUES (1,'Media/WhatsApp Documents/Report.pdf','application/pdf',0,
            'Report.pdf',NULL,NULL,NULL,NULL,3,'x',0);
        INSERT INTO message_media VALUES (2,'Media/WhatsApp Stickers/STK-1.webp','image/webp',0,
            NULL,NULL,64,64,NULL,NULL,'y',0);
        INSERT INTO message_media VALUES (3,'Media/WhatsApp Documents/Gone.pdf','application/pdf',0,
            'Gone.pdf',NULL,NULL,NULL,NULL,1,'z',0);
    """)
    conn.commit()
    conn.close()


@pytest.fixture
def repair_world(tmp_path):
    chat_db = tmp_path / "ChatStorage.sqlite"
    make_media_chatstorage(chat_db)
    add_whatsapp_examples(chat_db)
    msg = tmp_path / "msgstore.db"
    make_msgstore(msg)
    media = tmp_path / "Media"
    (media / "WhatsApp Documents").mkdir(parents=True)
    (media / "WhatsApp Stickers").mkdir(parents=True)
    png = tmp_path / "page.png"
    _png(png, 300, 400)
    subprocess.run(["sips", "-s", "format", "pdf", str(png), "--out", str(media / "WhatsApp Documents" / "Report.pdf")],
                   check=True, capture_output=True)
    _png(media / "WhatsApp Stickers" / "STK-1.webp", 512, 512)   # sips reads by content
    elsewhere = tmp_path / "Downloads" / "Gone.pdf"
    elsewhere.parent.mkdir()
    subprocess.run(["sips", "-s", "format", "pdf", str(png), "--out", str(elsewhere)], check=True, capture_output=True)
    return chat_db, msg, media, elsewhere


def _row(db: Path, stanza: str) -> dict:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    r = conn.execute(
        "SELECT m.ZMEDIASECTIONID, m.ZTEXT, m.ZFLAGS, i.ZMEDIALOCALPATH, i.ZXMPPTHUMBPATH, i.ZTITLE, "
        "i.ZAUTHORNAME, i.ZMOVIEDURATION, i.ZLATITUDE, i.ZLONGITUDE FROM ZWAMESSAGE m "
        "JOIN ZWAMEDIAITEM i ON i.Z_PK=m.ZMEDIAITEM WHERE m.ZSTANZAID=?", (stanza,)).fetchone()
    conn.close()
    return dict(r)


def _import(chat_db: Path, msg: Path):
    from wabridge.ios.chatstorage import Writer

    archive = load_archive(msg)
    writer = Writer(str(chat_db))
    writer.import_archive(archive, media_root=None, include_media=False)
    writer.finalize()
    return archive


def test_learns_document_and_sticker_conventions(repair_world):
    chat_db, *_ = repair_world
    conn = sqlite3.connect(chat_db)
    _flags, profiles = learn(conn, ours=set())
    conn.close()
    doc, sticker, voice = profiles[8], profiles[15], profiles[3]
    assert doc.section_style == "doc-" and doc.name_in_author and doc.text_from_name and doc.uses_pages
    assert doc.thumb_mimes == {"application/pdf"} and not doc.title_used
    assert sticker.section_style == "none" and sticker.uses_dims and not sticker.uses_thumb
    assert voice.section_style == "none"


def test_new_document_and_sticker_match_whatsapp(repair_world, tmp_path):
    chat_db, msg, media, _ = repair_world
    archive = _import(chat_db, msg)
    report = attach_media(chat_db, archive, media, tmp_path / "thumbs", out=lambda _l: None,
                          page_counts={"doc-1": 3})
    assert report.attached == {8: 1, 15: 1}
    doc = _row(chat_db, "doc-1")
    assert doc["ZMEDIASECTIONID"] == "doc-2023-11"
    assert (doc["ZAUTHORNAME"], doc["ZTEXT"], doc["ZTITLE"], doc["ZMOVIEDURATION"]) == ("Report.pdf", "Report", None, 3)
    assert doc["ZMEDIALOCALPATH"].endswith(".pdf") and doc["ZXMPPTHUMBPATH"].endswith(".thumb")
    assert (tmp_path / "thumbs" / doc["ZXMPPTHUMBPATH"]).read_bytes()[:2] == b"\xff\xd8"
    sticker = _row(chat_db, "stk-1")
    assert sticker["ZMEDIASECTIONID"] is None and sticker["ZXMPPTHUMBPATH"] is None
    assert (sticker["ZLONGITUDE"], sticker["ZLATITUDE"]) == (512.0, 512.0)   # real size, not Android's 64x64
    gone = _row(chat_db, "doc-found")                                        # no file: name still shown
    assert gone["ZMEDIALOCALPATH"] is None and gone["ZAUTHORNAME"] == "Gone.pdf"
    assert gone["ZMEDIASECTIONID"] == "doc-2023-11"


def test_repair_fixes_rows_from_an_earlier_stage(repair_world, tmp_path):
    """Stage 2 wrote documents/stickers the old way. The repair corrects them without re-adding files."""
    chat_db, msg, media, _ = repair_world
    archive = _import(chat_db, msg)
    conn = sqlite3.connect(chat_db)
    for stanza, path in (("doc-1", "Media/999@lid/1/2/12.pdf"), ("stk-1", "Media/999@lid/3/4/34.webp")):
        conn.execute("UPDATE ZWAMEDIAITEM SET ZMEDIALOCALPATH=?, ZTITLE='Report.pdf', ZLATITUDE=64, ZLONGITUDE=64 "
                     "WHERE Z_PK=(SELECT ZMEDIAITEM FROM ZWAMESSAGE WHERE ZSTANZAID=?)", (path, stanza))
        conn.execute("UPDATE ZWAMESSAGE SET ZMEDIASECTIONID='2023-11' WHERE ZSTANZAID=?", (stanza,))
    conn.commit()
    conn.close()

    report = attach_media(chat_db, archive, media, tmp_path / "thumbs", out=lambda _l: None)
    assert report.attached == {} and report.fixed == {8: 1, 15: 1}
    assert [j["relative_path"] for j in report.jobs] == ["Message/Media/999@lid/1/2/12.thumb"]  # only the new thumbnail
    doc = _row(chat_db, "doc-1")
    assert doc["ZMEDIALOCALPATH"] == "Media/999@lid/1/2/12.pdf"
    assert (doc["ZMEDIASECTIONID"], doc["ZAUTHORNAME"], doc["ZTITLE"]) == ("doc-2023-11", "Report.pdf", None)
    sticker = _row(chat_db, "stk-1")
    assert sticker["ZMEDIASECTIONID"] is None and (sticker["ZLONGITUDE"], sticker["ZLATITUDE"]) == (512.0, 512.0)


def test_found_file_is_attached(repair_world, tmp_path):
    chat_db, msg, media, elsewhere = repair_world
    archive = _import(chat_db, msg)
    report = attach_media(chat_db, archive, media, tmp_path / "thumbs", out=lambda _l: None,
                          extra_files={"doc-found": elsewhere})
    assert report.attached[8] == 2 and report.from_elsewhere == {8: 1}
    assert _row(chat_db, "doc-found")["ZMEDIALOCALPATH"].endswith(".pdf")


def test_archive_status_is_carried_and_never_removed(repair_world):
    chat_db, msg, *_ = repair_world
    archive = _import(chat_db, msg)
    conn = sqlite3.connect(chat_db)
    conn.execute("UPDATE ZWACHATSESSION SET ZARCHIVED=1 WHERE ZCONTACTJID='111@lid'")  # archived on the iPhone
    conn.commit()
    conn.close()
    assert carry_archived(chat_db, archive) == 1
    assert carry_archived(chat_db, archive) == 0
    conn = sqlite3.connect(chat_db)
    states = dict(conn.execute("SELECT COALESCE(ZCONTACTIDENTIFIER, ZCONTACTJID), ZARCHIVED FROM ZWACHATSESSION"))
    conn.close()
    assert states[JID] == 1 and states["60111111111@s.whatsapp.net"] == 1
    assert states["60199999999@s.whatsapp.net"] in (0, None)


def test_one_year_rule(tmp_path, repair_world):
    _chat_db, msg, media, elsewhere = repair_world
    import base64
    import hashlib

    sha = base64.b64encode(hashlib.sha256(elsewhere.read_bytes()).digest()).decode()
    conn = sqlite3.connect(msg)
    conn.execute("UPDATE message_media SET file_hash=? WHERE message_row_id=3", (sha,))
    conn.commit()
    conn.close()
    now = time.time()
    collected = tmp_path / "collected.json"

    def run(last_opened, modified, policy):
        collected.write_text(json.dumps({"files": [{"sha256": sha, "local": str(elsewhere), "source": "mac",
                                                    "last_opened": last_opened, "modified": modified}]}))
        return recovered_for_messages(collected, msg, media, policy, now=now)[0]

    assert run(now - 30 * 86400, now - 900 * 86400, "opened") == {"doc-found": elsewhere}
    assert run(now - 400 * 86400, now - 900 * 86400, "opened") == {}
    assert run(None, now - 30 * 86400, "opened") == {}
    assert run(None, now - 30 * 86400, "recent") == {"doc-found": elsewhere}
    assert run(None, now - 900 * 86400, "all") == {"doc-found": elsewhere}


def test_full_repair_stage_passes_verify(repair_world, tmp_path):
    chat_db, msg, media, elsewhere = repair_world
    src = make_backup(tmp_path / "finder" / "UDIDR", chat_db)
    live = stage_ios(msg, src, tmp_path / "stage", out=lambda _l: None, media_root=media, archived=True)
    s = json.loads((tmp_path / "stage" / "stage.json").read_text())
    assert s["media"]["attached"] == {"document": 1, "sticker": 1} and s["archived_set"] == 1
    assert not [c for c in verify_work(tmp_path / "stage") if c.blocking]
    assert live.is_dir()


def test_left_out_bubbles_from_an_earlier_stage_get_corrected_fields(repair_world, tmp_path):
    """A document bubble from stage 1 with no file is left out of the new import, but still fixed."""
    from wamover.media import drop_undownloaded

    chat_db, msg, media, _ = repair_world
    archive = _import(chat_db, msg)                      # stage 1 wrote doc-found as an empty bubble
    conn = sqlite3.connect(chat_db)
    conn.execute("UPDATE ZWAMESSAGE SET ZMEDIASECTIONID='2023-11' WHERE ZSTANZAID='doc-found'")
    conn.commit()
    conn.close()
    conn = sqlite3.connect(msg)
    conn.execute("UPDATE message SET text_data=NULL WHERE key_id='doc-found'")   # no caption -> left out
    conn.commit()
    conn.close()
    full = load_archive(msg)
    kept, dropped = drop_undownloaded(full, media)
    assert dropped == 1
    report = attach_media(chat_db, kept, media, tmp_path / "thumbs", out=lambda _l: None, fields_only=full)
    assert report.not_in_chatstorage == 0
    gone = _row(chat_db, "doc-found")
    assert gone["ZMEDIASECTIONID"] == "doc-2023-11" and gone["ZAUTHORNAME"] == "Gone.pdf"
    assert gone["ZMEDIALOCALPATH"] is None
