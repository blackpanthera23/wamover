import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def android_db(tmp_path: Path) -> Path:
    path = tmp_path / "msgstore.db"
    c = sqlite3.connect(path)
    c.executescript(
        """
        CREATE TABLE jid (_id INTEGER PRIMARY KEY, raw_string TEXT, user TEXT, server TEXT);
        CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT,
            hidden INTEGER, archived INTEGER);
        CREATE TABLE jid_map (lid_row_id INTEGER, jid_row_id INTEGER);
        CREATE TABLE message (_id INTEGER PRIMARY KEY, chat_row_id INTEGER, from_me INTEGER,
            sender_jid_row_id INTEGER, timestamp INTEGER, text_data TEXT, message_type INTEGER,
            key_id TEXT);
        CREATE TABLE message_media (message_row_id INTEGER, file_path TEXT, mime_type TEXT,
            file_size INTEGER, media_name TEXT);
        INSERT INTO jid VALUES (1,'60123456789@s.whatsapp.net','60123456789','s.whatsapp.net');
        INSERT INTO jid VALUES (2,'111-222@g.us','111-222','g.us');
        INSERT INTO jid VALUES (3,'60198765432@s.whatsapp.net','60198765432','s.whatsapp.net');
        INSERT INTO jid VALUES (4,'999999@lid','999999','lid');
        INSERT INTO jid VALUES (5,'status@broadcast','status','broadcast');
        INSERT INTO jid_map VALUES (4, 1);
        INSERT INTO chat VALUES (1,1,NULL,0,0);
        INSERT INTO chat VALUES (2,2,'Family <group>',0,0);
        INSERT INTO chat VALUES (3,3,NULL,0,0);
        INSERT INTO chat VALUES (4,4,NULL,0,0);
        INSERT INTO chat VALUES (5,5,NULL,0,0);
        INSERT INTO message VALUES (1,1,1,NULL,1700000000000,'hello',0,'k1');
        INSERT INTO message VALUES (2,1,0,1,1700000060000,'hi back',0,'k2');
        INSERT INTO message VALUES (3,2,0,3,1700000120000,'photo',1,'k3');
        INSERT INTO message VALUES (4,4,0,4,1700000180000,'via lid',0,'k4');
        INSERT INTO message VALUES (5,5,1,NULL,1700000200000,'story',0,'k5');
        INSERT INTO message_media VALUES (3,'Media/WhatsApp Images/a.jpg','image/jpeg',1234,NULL);
        """
    )
    c.commit()
    c.close()
    return path
