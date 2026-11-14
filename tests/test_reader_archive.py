import json
import sqlite3

import pytest

from wamover.android_reader import UnsupportedSchemaError, read_android_db
from wamover.archive import export_html, export_json
from wamover.safety import sha256_file


def test_reader_counts(android_db):
    before = sha256_file(android_db)
    ex = read_android_db(android_db)
    assert len(ex.chats) == 2  # empty chat hidden; status chat dropped; LID merged
    assert ex.message_count == 4
    phone = next(c for c in ex.chats if c.jid.endswith("@s.whatsapp.net"))
    assert [m.text for m in phone.messages] == ["hello", "hi back", "via lid"]
    assert all(not c.jid.endswith("@lid") for c in ex.chats)
    assert all("status@" not in c.jid for c in ex.chats)
    group = next(c for c in ex.chats if c.is_group)
    assert group.subject == "Family <group>"
    assert group.messages[0].media.mime_type == "image/jpeg"
    assert sha256_file(android_db) == before  # read-only


def test_unsupported_schema(tmp_path):
    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE messages (_id INTEGER)")
    c.commit()
    c.close()
    with pytest.raises(UnsupportedSchemaError, match="legacy"):
        read_android_db(p)


def test_exports(android_db, tmp_path):
    ex = read_android_db(android_db)
    j = export_json(ex, tmp_path / "out" / "chats.json")
    assert len(json.loads(j.read_text())) == 2
    idx = export_html(ex, tmp_path / "out" / "html")
    page = "".join(p.read_text() for p in idx.parent.glob("*.html"))
    assert "Family &lt;group&gt;" in page and "<group>" not in page


def test_invalid_utf8_in_a_message_does_not_stop_the_read(android_db):
    conn = sqlite3.connect(android_db)
    conn.execute(
        "INSERT INTO message VALUES (9,1,0,1,1700000300000,CAST(X'6F6B20F09F98FFFE0A21' AS TEXT),0,'k9')"
    )
    conn.commit()
    conn.close()
    ex = read_android_db(android_db)
    phone = next(c for c in ex.chats if c.jid.endswith("@s.whatsapp.net"))
    broken = next(m for m in phone.messages if m.text and m.text.startswith("ok "))
    assert "\ufffd" in broken.text and broken.text.endswith("!")
    assert ex.message_count == 5


def test_unsupported_type_without_text_is_left_out(android_db):
    conn = sqlite3.connect(android_db)
    conn.execute("INSERT INTO message VALUES (7,1,1,NULL,1700000400000,NULL,99,'poll-1')")
    conn.execute("INSERT INTO message VALUES (8,1,1,NULL,1700000500000,'a poll',99,'poll-2')")
    conn.commit()
    conn.close()
    ex = read_android_db(android_db)
    phone = next(c for c in ex.chats if c.jid.endswith("@s.whatsapp.net"))
    texts = [m.text for m in phone.messages]
    assert "a poll" in texts and all(t is None or "Unsupported" not in t for t in texts)
    assert ex.message_count == 5
