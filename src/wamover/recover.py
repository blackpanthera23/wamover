"""Find missing WhatsApp attachments elsewhere on the Mac or the Android phone. Read-only on both.

Android's msgstore.db keeps WhatsApp's own SHA-256 of every attachment (message_media.file_hash),
even for files that were never downloaded. A file found anywhere with exactly that SHA-256 is the
same file, whatever its name. Only exact matches are kept.

Mac: files that macOS has offloaded to iCloud (dataless) are skipped, so nothing gets downloaded.
Phone: only `find`, `stat` and `sha256sum` run over adb; matched files are copied with `adb pull`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shlex
import sqlite3
import stat as statmod
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from wabridge.android import adb

KIND = {1: "photo", 2: "voice/audio", 3: "video", 9: "document", 13: "GIF", 20: "sticker"}
SF_DATALESS = 0x40000000
PHONE_DIRS = ["/sdcard/DCIM", "/sdcard/Pictures", "/sdcard/Download", "/sdcard/Documents",
              "/sdcard/Movies", "/sdcard/Recordings", "/sdcard/Music"]
SKIP_PHONE = ("/sdcard/Android/",)
SHA_BATCH = 40


class RecoverError(RuntimeError):
    pass


@dataclass
class Wanted:
    sha256_b64: str
    size: int | None
    messages: list[dict]


def wanted_attachments(msgstore: Path, media_root: Path) -> dict[str, Wanted]:
    """Attachments whose file is not in the pulled Media folder, keyed by WhatsApp's SHA-256."""
    have = set()
    for _d, _s, files in os.walk(media_root):
        have.update(files)
    conn = sqlite3.connect(f"file:{Path(msgstore).resolve()}?mode=ro", uri=True)
    out: dict[str, Wanted] = {}
    try:
        for key, mtype, ts, path, fhash, fsize, flen, chat in conn.execute("""
            SELECT m.key_id, m.message_type, m.timestamp, mm.file_path, mm.file_hash,
                   mm.file_size, mm.file_length, j.raw_string
            FROM message m JOIN message_media mm ON mm.message_row_id = m._id
            JOIN chat c ON c._id = m.chat_row_id JOIN jid j ON j._id = c.jid_row_id
            WHERE m.message_type IN (1,2,3,9,13,20) AND mm.file_hash IS NOT NULL AND mm.file_hash <> ''
              AND j.raw_string NOT LIKE '%broadcast' AND j.raw_string NOT LIKE '%@newsletter'"""):
            if path and path.rsplit("/", 1)[-1] in have:
                continue
            size = fsize or flen or None
            w = out.setdefault(fhash, Wanted(fhash, size, []))
            if w.size is None and size:
                w.size = size
            w.messages.append({"key_id": key, "kind": KIND[mtype], "timestamp_ms": ts, "chat": chat})
    finally:
        conn.close()
    return out


def _b64(digest: bytes) -> str:
    return base64.b64encode(digest).decode()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return _b64(h.digest())


def _last_opened(path: Path) -> float | None:
    """Spotlight's 'Last opened' date, as Unix seconds, if macOS recorded one."""
    r = subprocess.run(["mdls", "-raw", "-name", "kMDItemLastUsedDate", str(path)],
                       capture_output=True, text=True)
    text = r.stdout.strip()
    if r.returncode != 0 or not text or text == "(null)":
        return None
    from datetime import datetime

    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S %z").timestamp()
    except ValueError:
        return None


def scan_mac(folders: list[Path], wanted: dict[str, Wanted], out: Callable[[str], None] = print) -> list[dict]:
    sizes = {w.size for w in wanted.values() if w.size}
    unknown_size = any(w.size is None for w in wanted.values())
    found: list[dict] = []
    seen: set[str] = set()
    for folder in folders:
        if not folder.is_dir():
            out(f"  (not found: {folder})")
            continue
        hashed = skipped_cloud = 0
        for dirpath, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for name in files:
                p = Path(dirpath) / name
                try:
                    st = p.lstat()
                except OSError:
                    continue
                if not statmod.S_ISREG(st.st_mode) or st.st_size == 0:
                    continue
                if getattr(st, "st_flags", 0) & SF_DATALESS:
                    skipped_cloud += 1
                    continue
                if st.st_size not in sizes and not unknown_size:
                    continue
                if st.st_size not in sizes and st.st_size > 200 * 1024 * 1024:
                    continue
                try:
                    digest = _sha256_file(p)
                except OSError:
                    continue
                hashed += 1
                if digest in wanted and digest not in seen:
                    seen.add(digest)
                    found.append({"sha256": digest, "source": "mac", "path": str(p), "size": st.st_size,
                                  "modified": st.st_mtime, "last_opened": _last_opened(p)})
        out(f"  {folder}: {hashed} files fingerprinted, {skipped_cloud} iCloud-only files skipped")
    return found


def _phone(cmd: str, serial: str | None) -> str:
    return adb.shell(cmd, serial=serial, check=False)


def scan_phone(wanted: dict[str, Wanted], serial: str | None = None,
               out: Callable[[str], None] = print) -> list[dict]:
    sizes = {w.size for w in wanted.values() if w.size}
    unknown_size = any(w.size is None for w in wanted.values())
    listing = []
    for d in PHONE_DIRS:
        text = _phone(f"find {shlex.quote(d)} -type f -exec toybox stat -c '%s|%X|%Y|%n' {{}} + 2>/dev/null", serial)
        for line in text.splitlines():
            parts = line.strip().split("|", 3)
            if len(parts) != 4 or not parts[0].isdigit():
                continue
            size, atime, mtime, path = int(parts[0]), int(parts[1]), int(parts[2]), parts[3]
            if size == 0 or path.startswith(SKIP_PHONE):
                continue
            if size in sizes or (unknown_size and size <= 200 * 1024 * 1024):
                listing.append((size, atime, mtime, path))
    out(f"  phone: {len(listing)} candidate files to fingerprint")
    found: list[dict] = []
    seen: set[str] = set()
    for i in range(0, len(listing), SHA_BATCH):
        batch = listing[i:i + SHA_BATCH]
        text = _phone("sha256sum " + " ".join(shlex.quote(p) for _s, _a, _m, p in batch) + " 2>/dev/null", serial)
        meta = {p: (s, a, m) for s, a, m, p in batch}
        for line in text.splitlines():
            hexd, _, path = line.strip().partition("  ")
            if len(hexd) != 64 or path not in meta:
                continue
            digest = _b64(bytes.fromhex(hexd))
            if digest in wanted and digest not in seen:
                seen.add(digest)
                s, a, m = meta[path]
                found.append({"sha256": digest, "source": "phone", "path": path, "size": s,
                              "modified": m, "last_opened": None, "accessed": a})
    return found


def collect(found: list[dict], dest: Path, serial: str | None = None) -> list[dict]:
    """Copy matched files into dest (APFS clone on the Mac, adb pull from the phone), then re-check SHA-256."""
    dest.mkdir(parents=True, exist_ok=True)
    kept = []
    for f in found:
        name = f"{hashlib.sha1(f['sha256'].encode()).hexdigest()[:10]}-{Path(f['path']).name}"
        target = dest / name
        if not target.exists():
            if f["source"] == "mac":
                r = subprocess.run(["cp", "-c", f["path"], str(target)], capture_output=True)
                if r.returncode != 0:
                    subprocess.run(["cp", f["path"], str(target)], capture_output=True)
            else:
                adb.run(["pull", "-a", f["path"], str(target)], serial=serial, check=False)
        if target.is_file() and _sha256_file(target) == f["sha256"]:
            os.chmod(target, 0o600)
            kept.append({**f, "local": str(target)})
    return kept


POLICIES = ("opened", "recent", "all")


def recovered_for_messages(collected: Path, msgstore: Path, media_root: Path, policy: str = "opened",
                           days: int = 365, now: float | None = None) -> tuple[dict[str, Path], dict]:
    """key_id -> local file, for found files allowed by `policy`.

    opened: only files macOS says you opened in the last `days` days.
    recent: also files with no open record that were created or changed in the last `days` days.
    all:    every found file.
    """
    if policy not in POLICIES:
        raise RecoverError(f"policy must be one of {POLICIES}")
    files = json.loads(Path(collected).read_text(encoding="utf-8"))["files"]
    cutoff = (now or time.time()) - days * 86400
    wanted = wanted_attachments(msgstore, media_root)
    out: dict[str, Path] = {}
    counts = {"files_found": len(files), "files_used": 0, "files_skipped_not_opened": 0, "messages": 0}
    for f in files:
        opened = f.get("last_opened")
        allowed = (
            policy == "all"
            or (opened is not None and opened >= cutoff)
            or (policy == "recent" and opened is None and f.get("modified", 0) >= cutoff)
        )
        local = Path(f["local"])
        if not allowed or f["sha256"] not in wanted or not local.is_file() or _sha256_file(local) != f["sha256"]:
            counts["files_skipped_not_opened"] += int(not allowed)
            continue
        counts["files_used"] += 1
        for m in wanted[f["sha256"]].messages:
            out[m["key_id"]] = local
            counts["messages"] += 1
    return out, counts


def page_counts(msgstore: Path) -> dict[str, int]:
    conn = sqlite3.connect(f"file:{Path(msgstore).resolve()}?mode=ro", uri=True)
    try:
        if "page_count" not in {r[1] for r in conn.execute("PRAGMA table_info(message_media)")}:
            return {}  # older WhatsApp builds do not record page counts
        return {k: int(n) for k, n in conn.execute(
            "SELECT m.key_id, mm.page_count FROM message m JOIN message_media mm ON mm.message_row_id = m._id "
            "WHERE m.message_type = 9 AND mm.page_count > 0")}
    finally:
        conn.close()


def save(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".partial")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
