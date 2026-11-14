"""Builders for synthetic Android databases and Finder-style iPhone backups."""

import plistlib
import sqlite3
from pathlib import Path

from wabridge.ios.backup import CHATSTORAGE, WA_GROUP_DOMAIN, build_mbfile, file_id

CHAT_FID = file_id(WA_GROUP_DOMAIN, CHATSTORAGE)


def make_chatstorage(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE Z_PRIMARYKEY (Z_ENT INTEGER, Z_NAME TEXT, Z_SUPER INTEGER, Z_MAX INTEGER);
        INSERT INTO Z_PRIMARYKEY VALUES (1, 'WAChatSession', 0, 1);
        INSERT INTO Z_PRIMARYKEY VALUES (2, 'WAMessage', 0, 1);
        CREATE TABLE ZWACHATSESSION (
            Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER,
            ZCONTACTJID TEXT, ZCONTACTIDENTIFIER TEXT, ZPARTNERNAME TEXT,
            ZSESSIONTYPE INTEGER, ZMESSAGECOUNTER INTEGER, ZUNREADCOUNT INTEGER,
            ZARCHIVED INTEGER, ZHIDDEN INTEGER, ZREMOVED INTEGER, ZCONTACTABID INTEGER,
            ZFLAGS INTEGER, ZSPOTLIGHTSTATUS INTEGER, ZLASTMESSAGEDATE REAL,
            ZLASTMESSAGE INTEGER, ZLASTMESSAGETEXT TEXT,
            ZIDENTITYVERIFICATIONEPOCH INTEGER, ZIDENTITYVERIFICATIONSTATE INTEGER,
            ZGROUPINFO INTEGER
        );
        CREATE TABLE ZWAMESSAGE (
            Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER,
            ZCHATSESSION INTEGER, ZISFROMME INTEGER, ZMESSAGETYPE INTEGER,
            ZMESSAGESTATUS INTEGER, ZSORT INTEGER, ZMESSAGEDATE REAL, ZSENTDATE REAL,
            ZTEXT TEXT, ZSTANZAID TEXT, ZFROMJID TEXT, ZTOJID TEXT, ZPUSHNAME TEXT,
            ZFLAGS INTEGER, ZSPOTLIGHTSTATUS INTEGER, ZDATAITEMVERSION INTEGER,
            ZSTARRED INTEGER, ZMESSAGEERRORSTATUS INTEGER, ZDOCID INTEGER,
            ZCHILDMESSAGESDELIVEREDCOUNT INTEGER, ZCHILDMESSAGESPLAYEDCOUNT INTEGER,
            ZCHILDMESSAGESREADCOUNT INTEGER, ZFILTEREDRECIPIENTCOUNT INTEGER,
            ZENCRETRYCOUNT INTEGER, ZGROUPEVENTTYPE INTEGER
        );
        INSERT INTO ZWACHATSESSION VALUES (
            1,1,1,'60111111111@s.whatsapp.net','60111111111@s.whatsapp.net','Seed',
            0,1,0,0,0,0,0,272,-5,100,1,'seed',0,0,NULL
        );
        INSERT INTO ZWAMESSAGE VALUES (
            1,2,1,1,1,0,8,1,100,100,'seed-msg','seed-1',NULL,
            '60111111111@s.whatsapp.net',NULL,16777280,-32768,3,0,0,0,0,0,0,0,0,0
        );
        """
    )
    conn.commit()
    conn.close()


def make_msgstore(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE jid (_id INTEGER PRIMARY KEY, raw_string TEXT, user TEXT, server TEXT);
        CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT);
        CREATE TABLE message (_id INTEGER PRIMARY KEY, chat_row_id INTEGER, from_me INTEGER,
            sender_jid_row_id INTEGER, timestamp INTEGER, text_data TEXT, message_type INTEGER,
            key_id TEXT);
        INSERT INTO jid VALUES (1,'60123456789@s.whatsapp.net','60123456789','s.whatsapp.net');
        INSERT INTO chat VALUES (1,1,NULL);
        INSERT INTO message VALUES (1,1,1,NULL,1700000000000,'hello-from-android',0,'android-1');
        """
    )
    conn.commit()
    conn.close()


def make_backup(folder: Path, chat_db: Path, *, encrypted: bool = False) -> Path:
    folder.mkdir(parents=True)
    blob_dir = folder / CHAT_FID[:2]
    blob_dir.mkdir()
    payload = chat_db.read_bytes()
    (blob_dir / CHAT_FID).write_bytes(payload)
    conn = sqlite3.connect(folder / "Manifest.db")
    conn.execute(
        "CREATE TABLE Files (fileID TEXT PRIMARY KEY, domain TEXT, relativePath TEXT, flags INTEGER, file BLOB)"
    )
    conn.execute(
        "INSERT INTO Files VALUES (?,?,?,?,?)",
        (CHAT_FID, WA_GROUP_DOMAIN, CHATSTORAGE, 1,
         build_mbfile(CHATSTORAGE, len(payload), 0o100644, 1, 1)),
    )
    conn.commit()
    conn.close()
    (folder / "Manifest.plist").write_bytes(plistlib.dumps({"IsEncrypted": encrypted}))
    (folder / "Info.plist").write_bytes(plistlib.dumps({"Device Name": "Test iPhone"}))
    (folder / "Status.plist").write_bytes(plistlib.dumps({"BackupState": "new"}))
    return folder


def chat_blob(folder: Path) -> Path:
    return folder / CHAT_FID[:2] / CHAT_FID


def count_text(db: Path, text: str) -> int:
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT COUNT(*) FROM ZWAMESSAGE WHERE ZTEXT=?", (text,)).fetchone()[0]
    finally:
        conn.close()


def prepared_work(tmp_path: Path, udid: str = "UDID123") -> tuple[Path, Path]:
    """Run ios-prepare on synthetic data. Returns (work_dir, original Finder-style backup)."""
    from wamover.ios_prepare import stage_ios

    original_db = tmp_path / "orig.sqlite"
    make_chatstorage(original_db)
    src = make_backup(tmp_path / "src" / udid, original_db)
    msg = tmp_path / "msgstore.db"
    make_msgstore(msg)
    work = tmp_path / "work"
    stage_ios(msg, src, work)
    return work, src
