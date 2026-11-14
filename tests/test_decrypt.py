import pytest

from wamover.decrypt import DecryptError, decrypt_backup, detect_backup, resolve_key


def test_detect(tmp_path):
    p = tmp_path / "msgstore.db.crypt15"
    p.write_bytes(b"x")
    assert detect_backup(p).crypt_version == 15
    with pytest.raises(DecryptError):
        detect_backup(tmp_path / "nope.crypt15")
    q = tmp_path / "msgstore.db"
    q.write_bytes(b"x")
    with pytest.raises(DecryptError):
        detect_backup(q)


def test_resolve_key(tmp_path):
    assert resolve_key("AB" * 32) == "ab" * 32
    assert resolve_key("ab cd " * 16) == "abcd" * 16
    with pytest.raises(DecryptError):
        resolve_key("not-a-key")


def test_bad_key_leaves_source_untouched(tmp_path):
    p = tmp_path / "msgstore.db.crypt15"
    p.write_bytes(b"garbage" * 100)
    before = p.read_bytes()
    with pytest.raises(DecryptError):
        decrypt_backup(p, "00" * 32, tmp_path / "work")
    assert p.read_bytes() == before
    assert not (tmp_path / "work" / "msgstore.db").exists()
    assert not (tmp_path / "work" / "msgstore.db.partial").exists()


def test_crypt15_roundtrip_keeps_source_and_skips_shell(tmp_path, monkeypatch):
    import sqlite3
    import subprocess

    db = tmp_path / "plain.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()
    from wabridge.android.crypt15 import encrypt_bytes

    key = "ab" * 32
    src = tmp_path / "msgstore.db.crypt15"
    src.write_bytes(encrypt_bytes(db.read_bytes(), key))
    before = src.read_bytes()

    def boom(*_a, **_k):
        raise AssertionError("decryption must not spawn a process")

    monkeypatch.setattr(subprocess, "run", boom)
    out = decrypt_backup(src, " ".join(key[i : i + 4] for i in range(0, 64, 4)), tmp_path / "work")
    assert out.read_bytes()[:16] == b"SQLite format 3\x00"
    assert src.read_bytes() == before


def _encrypted_db(tmp_path, key, *, rows=3, blob=0, compress=True):
    import sqlite3

    from wabridge.android.crypt15 import encrypt_bytes

    db = tmp_path / "plain.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (x BLOB)")
    for i in range(rows):
        conn.execute("INSERT INTO t VALUES (?)", (__import__("os").urandom(blob) if blob else str(i),))
    conn.commit()
    conn.close()
    src = tmp_path / "msgstore.db.crypt15"
    src.write_bytes(encrypt_bytes(db.read_bytes(), key, compress=compress))
    return src, db


@pytest.mark.parametrize("compress", [True, False])
def test_crypt15_larger_than_one_chunk(tmp_path, compress):
    key = "cd" * 32
    src, plain = _encrypted_db(tmp_path, key, rows=3, blob=3 * 1024 * 1024, compress=compress)
    assert src.stat().st_size > 2 * 4 * 1024 * 1024  # spans several 4 MB chunks
    out = decrypt_backup(src, key, tmp_path / "work")
    assert out.read_bytes() == plain.read_bytes()


def test_crypt15_wrong_key_leaves_nothing(tmp_path):
    src, _ = _encrypted_db(tmp_path, "cd" * 32)
    before = src.read_bytes()
    with pytest.raises(DecryptError, match="has to belong to this backup"):
        decrypt_backup(src, "ef" * 32, tmp_path / "work")
    assert src.read_bytes() == before
    assert list((tmp_path / "work").iterdir()) == []
