"""Decrypt an Android WhatsApp backup without modifying the source.

crypt15 (the end-to-end encrypted local backup) is decrypted in-process, so the
64-digit key never appears on a command line. crypt12/14 still need the binary
key file from the phone; a typed key is refused for those rather than passed
to another process.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .safety import assert_unchanged, sha256_file

_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
_CRYPT = re.compile(r"\.crypt(12|14|15)$")
_SPACES = re.compile(r"[\s\-]")


class DecryptError(RuntimeError):
    pass


@dataclass
class BackupInfo:
    path: Path
    crypt_version: int  # 12, 14 or 15
    size: int


def detect_backup(path: Path) -> BackupInfo:
    path = Path(path)
    if not path.is_file():
        raise DecryptError(f"Backup file not found: {path}")
    m = _CRYPT.search(path.name)
    if not m:
        raise DecryptError(
            f"Cannot detect backup format from name '{path.name}'. "
            "Expected msgstore.db.crypt12, .crypt14 or .crypt15."
        )
    return BackupInfo(path=path, crypt_version=int(m.group(1)), size=path.stat().st_size)


def resolve_key(key: str) -> str:
    """Return a normalised 64-digit hex key, or a path to an existing key file."""
    raw = key.strip()
    compact = _SPACES.sub("", raw)
    if _HEX64.match(compact):
        return compact.lower()
    p = Path(raw).expanduser()
    if p.is_file():
        return str(p)
    raise DecryptError(
        "Key must be the 64-digit key WhatsApp shows (spaces are fine) or a path to "
        "an existing key file (encrypted_backup.key for crypt15, 'key' for crypt12/14)."
    )


def _is_sqlite(path: Path) -> bool:
    if not path.is_file():
        return False
    with open(path, "rb") as f:
        return f.read(16) == b"SQLite format 3\x00"


CHUNK = 4 * 1024 * 1024
GCM_TAG_LEN = 16


def _stream_once(src: Path, aes_key: bytes, iv: bytes, start: int, end: int, tag: bytes, dest: Path) -> None:
    """Decrypt src[start:end] with AES-GCM in chunks, inflate it, write dest.

    Raises ValueError when the first bytes do not look like zlib/SQLite (wrong IV)
    and cryptography's InvalidTag when authentication fails at the end.
    """
    import zlib

    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    decryptor = Cipher(algorithms.AES(aes_key), modes.GCM(iv)).decryptor()
    inflater = None
    first = True
    with open(src, "rb") as fin, open(dest, "wb") as fout:
        fin.seek(start)
        remaining = end - start
        while remaining > 0:
            block = fin.read(min(CHUNK, remaining))
            if not block:
                raise ValueError("backup file ended early")
            remaining -= len(block)
            plain = decryptor.update(block)
            if first:
                first = False
                if plain[:1] == b"\x78":
                    inflater = zlib.decompressobj()
                elif not plain.startswith(b"SQLite format 3\x00"):
                    raise ValueError("first bytes are not a database")
            fout.write(inflater.decompress(plain) if inflater else plain)
        decryptor.finalize_with_tag(tag)
        if inflater:
            fout.write(inflater.flush())


def _decrypt_crypt15(src: Path, key: str, dest: Path) -> None:
    """Chunked crypt15 decryption, so a multi-GB backup never has to fit in memory.

    Key derivation and header parsing come from WaBridge. WaBridge's own decryptor
    decrypts the whole file in one call, which fails above 2 GB.
    """
    from cryptography.exceptions import InvalidTag
    from wabridge.android.crypt15 import Crypt15Error, derive_aes_key, parse_header, parse_key

    try:
        aes_key = derive_aes_key(parse_key(key))
        with open(src, "rb") as fh:
            header = parse_header(fh.read(8192))
    except Crypt15Error as exc:
        raise DecryptError("Decryption failed. This does not look like a crypt15 backup.\n" + str(exc)) from exc

    size = src.stat().st_size
    start = header.payload_offset
    last_error = "no candidate IV matched"
    for iv in header.iv_candidates:
        # Some builds append a 16-byte trailer after the GCM tag; try the plain layout first.
        for trailer in (0, 16):
            end = size - trailer - GCM_TAG_LEN
            if end <= start:
                raise DecryptError("Decryption failed. The backup file is too small.")
            with open(src, "rb") as fh:
                fh.seek(end)
                tag = fh.read(GCM_TAG_LEN)
            try:
                _stream_once(src, aes_key, iv, start, end, tag, dest)
            except InvalidTag:
                last_error = "authentication failed (wrong key, or the file is damaged)"
                continue
            except ValueError as exc:
                last_error = str(exc)
                break  # wrong IV: the trailer does not matter, try the next IV
            if not _is_sqlite(dest):
                dest.unlink(missing_ok=True)
                raise DecryptError("Decryption did not produce a database. The key does not match this backup.")
            return
    dest.unlink(missing_ok=True)
    raise DecryptError(f"Decryption failed. The 64-digit key has to belong to this backup. ({last_error})")


def _decrypt_legacy(src: Path, key_file: str, dest: Path) -> None:
    """crypt12/14. The argument is a key-file path, never the key itself."""
    cmd = [sys.executable, "-m", "wa_crypt_tools.wadecrypt", key_file, str(src), str(dest)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    # wadecrypt exits 0 even on a wrong key, so the SQLite header is the real check.
    if proc.returncode != 0 or not _is_sqlite(dest):
        dest.unlink(missing_ok=True)
        detail = (proc.stderr or proc.stdout).strip()
        raise DecryptError("Decryption failed. Check the key file matches this backup.\n" + detail)


def decrypt_backup(backup: Path, key: str, work_dir: Path, overwrite: bool = False) -> Path:
    """Decrypt `backup` into work_dir/msgstore.db and return that path."""
    info = detect_backup(backup)
    key_ref = resolve_key(key)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    out = work_dir / "msgstore.db"
    if out.exists() and not overwrite:
        raise DecryptError(f"{out} already exists. Use --overwrite to replace it.")
    if out.resolve() == info.path.resolve():
        raise DecryptError("Output path must differ from the source backup.")

    before = sha256_file(info.path)
    tmp = work_dir / "msgstore.db.partial"
    tmp.unlink(missing_ok=True)
    try:
        if info.crypt_version == 15:
            _decrypt_crypt15(info.path, key_ref, tmp)
        elif _HEX64.match(key_ref):
            raise DecryptError(
                "A 64-digit key only decrypts a crypt15 backup. "
                "crypt12/14 need the binary 'key' file from the phone, passed as --key /path/to/key."
            )
        else:
            _decrypt_legacy(info.path, key_ref, tmp)
        assert_unchanged(info.path, before)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(out)
    return out
