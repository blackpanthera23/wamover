"""Group membership and chat-list order, from what went wrong on a real iPhone."""

import json
import sqlite3
from pathlib import Path

import pytest
from helpers import make_backup

from wamover.android_reader import load_archive
from wamover.groups import (
    GroupError,
    check_new_members,
    deactivate_foreign_members,
    prepare_groups,
)
from wamover.ios_prepare import apply_negative_zsort, stage_ios

GROUP = "120363000000000001@g.us"
ALI_PHONE, ALI_LID = "60123456789@s.whatsapp.net", "111111@lid"
BOB_PHONE = "60198765432@s.whatsapp.net"
ME_PHONE, ME_LID = "60100000000@s.whatsapp.net", "999999@lid"


def make_chatstorage(path: Path) -> None:
    """WhatsApp's own state: you are in GROUP; members by LID; your phone row inactive; a 'left' notice."""
    conn = sqlite3.connect(path)
    conn.executescript(f"""
        CREATE TABLE Z_PRIMARYKEY (Z_ENT INTEGER, Z_NAME TEXT, Z_SUPER INTEGER, Z_MAX INTEGER);
        INSERT INTO Z_PRIMARYKEY VALUES (1,'WAChatSession',0,2),(2,'WAMessage',0,3),(5,'WAGroupMember',0,3),
                                        (6,'WAGroupInfo',0,1);
        CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER,
            ZCONTACTJID TEXT, ZCONTACTIDENTIFIER TEXT, ZPARTNERNAME TEXT, ZSESSIONTYPE INTEGER,
            ZMESSAGECOUNTER INTEGER, ZUNREADCOUNT INTEGER, ZARCHIVED INTEGER, ZHIDDEN INTEGER,
            ZREMOVED INTEGER, ZCONTACTABID INTEGER, ZFLAGS INTEGER, ZSPOTLIGHTSTATUS INTEGER,
            ZLASTMESSAGEDATE REAL, ZLASTMESSAGE INTEGER, ZLASTMESSAGETEXT TEXT,
            ZIDENTITYVERIFICATIONEPOCH INTEGER, ZIDENTITYVERIFICATIONSTATE INTEGER, ZGROUPINFO INTEGER);
        CREATE TABLE ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER,
            ZCHATSESSION INTEGER, ZISFROMME INTEGER, ZMESSAGETYPE INTEGER, ZMESSAGESTATUS INTEGER,
            ZSORT INTEGER, ZMESSAGEDATE REAL, ZSENTDATE REAL, ZTEXT TEXT, ZSTANZAID TEXT, ZFROMJID TEXT,
            ZTOJID TEXT, ZPUSHNAME TEXT, ZFLAGS INTEGER, ZSPOTLIGHTSTATUS INTEGER, ZDATAITEMVERSION INTEGER,
            ZSTARRED INTEGER, ZMESSAGEERRORSTATUS INTEGER, ZDOCID INTEGER, ZCHILDMESSAGESDELIVEREDCOUNT INTEGER,
            ZCHILDMESSAGESPLAYEDCOUNT INTEGER, ZCHILDMESSAGESREADCOUNT INTEGER, ZFILTEREDRECIPIENTCOUNT INTEGER,
            ZENCRETRYCOUNT INTEGER, ZGROUPEVENTTYPE INTEGER, ZGROUPMEMBER INTEGER, ZLASTSESSION INTEGER);
        CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER,
            ZCONTACTABID INTEGER, ZISACTIVE INTEGER, ZISADMIN INTEGER, ZSENDERKEYSENT INTEGER,
            ZCHATSESSION INTEGER, ZRECENTGROUPCHAT INTEGER, ZCONTACTIDENTIFIER TEXT, ZCONTACTNAME TEXT,
            ZFIRSTNAME TEXT, ZMEMBERJID TEXT);
        CREATE TABLE ZWAGROUPINFO (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZSTATE INTEGER,
            ZCHATSESSION INTEGER, ZCREATIONDATE REAL, ZGENERATION INTEGER);
        INSERT INTO ZWACHATSESSION (Z_PK,Z_ENT,Z_OPT,ZCONTACTJID,ZPARTNERNAME,ZSESSIONTYPE,ZFLAGS,ZSPOTLIGHTSTATUS,
            ZMESSAGECOUNTER,ZGROUPINFO,ZLASTMESSAGE,ZLASTMESSAGEDATE) VALUES
            (1,1,1,'{GROUP}','Family',1,256,1,1,1,1,816000000),
            (2,1,1,'500@lid','Old friend',0,272,-5,1,NULL,3,816000000);
        INSERT INTO ZWAGROUPINFO VALUES (1,6,1,0,1,600000000,0);
        INSERT INTO ZWAGROUPMEMBER (Z_PK,Z_ENT,Z_OPT,ZISACTIVE,ZISADMIN,ZCHATSESSION,ZCONTACTNAME,ZMEMBERJID) VALUES
            (1,5,1,1,0,1,'Ali','{ALI_LID}'), (2,5,1,1,0,1,'Me','{ME_LID}'), (3,5,1,0,0,1,'Me','{ME_PHONE}');
        INSERT INTO ZWAMESSAGE (Z_PK,Z_ENT,Z_OPT,ZCHATSESSION,ZISFROMME,ZMESSAGETYPE,ZMESSAGESTATUS,ZSORT,
            ZMESSAGEDATE,ZTEXT,ZSTANZAID,ZFLAGS,ZGROUPEVENTTYPE) VALUES
            (1,2,1,1,0,6,0,1,816000000,NULL,'wa-notice-1',16777216,3),
            (2,2,1,1,1,0,8,2,815000000,'hi from iPhone','wa-text',16777280,0),
            (3,2,1,2,0,10,0,3,816000000,NULL,'wa-notice-2',16777216,38);
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
        CREATE TABLE jid_map (lid_row_id INTEGER, jid_row_id INTEGER);
        CREATE TABLE group_participant_user (group_jid_row_id INTEGER, user_jid_row_id INTEGER, rank INTEGER);
        INSERT INTO jid VALUES (1,'{GROUP}','120363000000000001','g.us'),(2,'{ALI_PHONE}','60123456789','s.whatsapp.net'),
                               (3,'{ALI_LID}','111111','lid'),(4,'{BOB_PHONE}','60198765432','s.whatsapp.net'),
                               (5,'lid_me','lid_me','lid_me'),(6,'500@lid','500','lid');
        INSERT INTO jid_map VALUES (3,2);
        INSERT INTO chat VALUES (1,1,'Family'),(2,6,NULL);
        INSERT INTO group_participant_user VALUES (1,2,0),(1,4,0),(1,5,2);
        INSERT INTO message VALUES (1,1,0,2,1700000000000,'from ali',0,'a-1');
        INSERT INTO message VALUES (2,1,0,4,1700000060000,'from bob',0,'b-1');
        INSERT INTO message VALUES (3,1,1,NULL,1700000120000,'from me',0,'m-1');
        INSERT INTO message VALUES (4,2,0,6,1600000000000,'old 1:1 message',0,'o-1');
    """)
    conn.commit()
    conn.close()


@pytest.fixture
def world(tmp_path):
    db = tmp_path / "ChatStorage.sqlite"
    make_chatstorage(db)
    msg = tmp_path / "msgstore.db"
    make_msgstore(msg)
    return db, msg


def test_prepare_groups_drops_participants_and_maps_senders(world):
    db, msg = world
    archive, report = prepare_groups(load_archive(msg), msg, db)
    group = next(c for c in archive.chats if c.is_group)
    assert group.participants == []
    assert report.participant_rows_not_imported == 3
    senders = {m.key_id: m.sender_jid for m in group.messages}
    assert senders["a-1"] == ALI_LID                 # linked to WhatsApp's own row
    assert senders["b-1"] == BOB_PHONE               # not in WhatsApp's list: stays, becomes inactive
    assert report.senders_linked_to_whatsapp_member == 1


def test_lid_me_is_never_a_member(world):
    db, _ = world
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO ZWAGROUPMEMBER (Z_PK,Z_ENT,Z_OPT,ZISACTIVE,ZCHATSESSION,ZMEMBERJID) "
                 "VALUES (9,5,1,1,1,'lid_me')")
    conn.commit()
    conn.close()
    from wamover.groups import GroupReport
    with pytest.raises(GroupError):
        check_new_members(db, 3, GroupReport())


def test_stage_adds_no_active_members_and_sorts_list_by_real_messages(world, tmp_path):
    db, msg = world
    src = make_backup(tmp_path / "finder" / "UDIDG", db)
    live = stage_ios(msg, src, tmp_path / "stage", out=lambda _l: None)
    stage = json.loads((tmp_path / "stage" / "stage.json").read_text())
    assert stage["groups"]["participant_rows_not_imported"] == 3
    from helpers import chat_blob
    conn = sqlite3.connect(chat_blob(live))
    members = conn.execute("SELECT ZMEMBERJID, ZISACTIVE FROM ZWAGROUPMEMBER ORDER BY Z_PK").fetchall()
    ali_row = conn.execute("SELECT g.ZMEMBERJID FROM ZWAMESSAGE m JOIN ZWAGROUPMEMBER g ON g.Z_PK = m.ZGROUPMEMBER "
                           "WHERE m.ZSTANZAID = 'a-1'").fetchone()[0]
    group = conn.execute("SELECT ZLASTMESSAGEDATE, (SELECT ZSTANZAID FROM ZWAMESSAGE WHERE Z_PK = s.ZLASTMESSAGE) "
                         "FROM ZWACHATSESSION s WHERE ZCONTACTJID = ?", (GROUP,)).fetchone()
    old = conn.execute("SELECT ZLASTMESSAGEDATE FROM ZWACHATSESSION WHERE ZCONTACTJID = '500@lid'").fetchone()[0]
    conn.close()
    assert members[:3] == [(ALI_LID, 1), (ME_LID, 1), (ME_PHONE, 0)]          # WhatsApp's rows untouched
    assert [m for m in members[3:]] == [(BOB_PHONE, 0)]                      # one new row, inactive, no lid_me
    assert ali_row == ALI_LID
    assert group[1] == "wa-text"                     # newest real message, not the newer 'left' notice
    assert group[0] == 815000000
    assert abs(old - (1600000000 - 978307200)) < 1   # old chat sinks to its real date


def test_cleanup_sets_foreign_members_inactive_only(world):
    db, _ = world
    conn = sqlite3.connect(db)
    conn.executescript(f"""
        INSERT INTO ZWAGROUPMEMBER (Z_PK,Z_ENT,Z_OPT,ZISACTIVE,ZCHATSESSION,ZMEMBERJID) VALUES
            (10,5,1,1,1,'{BOB_PHONE}'), (11,5,1,1,1,'lid_me'), (12,5,1,1,1,'222222@lid');
    """)
    conn.commit()
    conn.close()
    assert deactivate_foreign_members(db) == 2
    conn = sqlite3.connect(db)
    state = dict(conn.execute("SELECT ZMEMBERJID, ZISACTIVE FROM ZWAGROUPMEMBER"))
    conn.close()
    assert state[BOB_PHONE] == 0 and state["lid_me"] == 0
    assert state[ALI_LID] == 1 and state[ME_LID] == 1 and state["222222@lid"] == 1


def test_cleanup_skips_builds_that_use_phone_numbers(tmp_path):
    db = tmp_path / "old.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZISACTIVE INTEGER, ZMEMBERJID TEXT);
        INSERT INTO ZWAGROUPMEMBER VALUES (1,1,'60123456789@s.whatsapp.net');
    """)
    conn.commit()
    conn.close()
    assert deactivate_foreign_members(db) == 0


def test_chat_with_only_notices_keeps_its_notice(world):
    db, _ = world
    apply_negative_zsort(db)
    conn = sqlite3.connect(db)
    last = conn.execute("SELECT (SELECT ZSTANZAID FROM ZWAMESSAGE WHERE Z_PK = s.ZLASTMESSAGE) FROM ZWACHATSESSION s "
                        "WHERE ZCONTACTJID = '500@lid'").fetchone()[0]
    conn.close()
    assert last == "wa-notice-2"
