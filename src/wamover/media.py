"""Attach Android media files and links to imported messages, the way WhatsApp on this iPhone does it.

Every convention here is copied from rows WhatsApp itself wrote on the user's iPhone (read from
the pristine backup), never from guesses:

- File: `Media/<chat ZCONTACTJID>/<u0>/<u1>/<uuid>.<ext>` in ZMEDIALOCALPATH; the backup holds it
  at `Message/` + that path.
- ZVCARDNAME = base64 SHA-256 of the file, ZVCARDSTRING = MIME type, ZFILESIZE = bytes,
  ZLATITUDE/ZLONGITUDE = height/width for pictures and video.
- Thumbnail: a small JPEG (longest side ~96 px) at the same path with `.thumb`, in ZXMPPTHUMBPATH.
- Message ZFLAGS for a media message with a downloaded file, per type, direction and chat kind.
- Links: one WAMessageDataItem row per URL (ZTYPE 0, ZCHATJID "p<session Z_PK>").

Which columns a type uses (hash, MIME, size, dimensions, thumbnail) is learned per type from those
rows. A media type with no WhatsApp-made example on the iPhone is skipped and counted.
"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from wabridge.ios.chatstorage import COCOA_EPOCH_OFFSET
from wabridge.model import Archive, MsgKind

from .archive import MediaResolver

IOS_MEDIA_TYPE = {
    MsgKind.IMAGE: 1, MsgKind.VIDEO: 2, MsgKind.AUDIO: 3, MsgKind.VOICE: 3,
    MsgKind.DOCUMENT: 8, MsgKind.GIF: 11, MsgKind.STICKER: 15,
}
TYPE_NAME = {1: "photo", 2: "video", 3: "voice/audio", 8: "document", 11: "GIF", 15: "sticker"}
THUMB_KINDS = {1: "image", 2: "video", 11: "video", 15: "image", 8: "pdf"}  # sips renders page 1 of a PDF
THUMB_REQUIRED = {1, 2, 11}   # WhatsApp kept a thumbnail for every photo, video and GIF it downloaded
THUMB_MAX = 96
THUMB_TIMEOUT = 60
VIDEO_THUMB_TIMEOUT = 20  # normal videos take under 1 s; a few files make Quick Look hang
# Quick Look thumbnails for video are slow and can hang when many run at once.
_VIDEO_SLOTS = threading.BoundedSemaphore(2)
NAMESPACE = uuid.UUID("7c1d6b2e-5a35-4f43-9b2f-2b9f7a0e8f11")
URL_RE = re.compile(r"(https?://[^\s<>\"']+|www\.[^\s<>\"']+)", re.IGNORECASE)
SPECIAL_JIDS = ("status", "newsletter", "broadcast")


class MediaError(RuntimeError):
    pass


@dataclass
class TypeProfile:
    """What WhatsApp on this iPhone stores for one media type."""

    uses_hash: bool = False
    uses_mime: bool = False
    uses_dims: bool = False
    uses_thumb: bool = False
    examples: int = 0
    sections: Counter = field(default_factory=Counter)   # "month", "doc-", ... or "none"
    thumb_mimes: set = field(default_factory=set)        # MIME types that had a thumbnail
    name_in_author: bool = False                          # documents: file name in ZAUTHORNAME
    title_used: bool = False                              # ZTITLE ever set (captions)
    text_rows: int = 0                                    # rows with ZTEXT and a file name
    text_is_stem: int = 0                                 # ... where ZTEXT is the name without extension
    uses_pages: bool = False                              # documents: page count in ZMOVIEDURATION

    @property
    def section_style(self) -> str:
        return self.sections.most_common(1)[0][0] if self.sections else "month"

    @property
    def text_from_name(self) -> bool:
        return self.text_rows > 0 and self.text_is_stem == self.text_rows


@dataclass
class MediaReport:
    attached: Counter = field(default_factory=Counter)
    no_file_on_android: Counter = field(default_factory=Counter)
    no_reference: Counter = field(default_factory=Counter)
    no_thumb: Counter = field(default_factory=Counter)
    fixed: Counter = field(default_factory=Counter)
    from_elsewhere: Counter = field(default_factory=Counter)
    fields_corrected: int = 0
    already_had_file: int = 0
    not_in_chatstorage: int = 0
    thumbs: int = 0
    thumbs_failed: int = 0
    links_added: int = 0
    bytes: int = 0
    jobs: list[dict] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "attached": {TYPE_NAME.get(k, k): v for k, v in sorted(self.attached.items())},
            "no_file_on_android": {TYPE_NAME.get(k, k): v for k, v in sorted(self.no_file_on_android.items())},
            "skipped_no_iphone_example": {
                f"{TYPE_NAME.get(t, t)}, {'sent' if me else 'received'}, {'group' if grp else '1:1'}": v
                for (t, me, grp), v in sorted(self.no_reference.items())
            },
            "already_had_file": self.already_had_file,
            "existing_files_fixed": {TYPE_NAME.get(k, k): v for k, v in sorted(self.fixed.items())},
            "files_found_elsewhere": {TYPE_NAME.get(k, k): v for k, v in sorted(self.from_elsewhere.items())},
            "messages_with_fields_corrected": self.fields_corrected,
            "not_in_chatstorage": self.not_in_chatstorage,
            "thumbnails": self.thumbs,
            "thumbnails_failed": self.thumbs_failed,
            "skipped_no_thumbnail": {TYPE_NAME.get(k, k): v for k, v in sorted(self.no_thumb.items())},
            "links_added": self.links_added,
            "media_bytes": self.bytes,
        }


# ---------------------------------------------------------------------- learning from WhatsApp's rows

def _special(jid: str | None) -> bool:
    return bool(jid) and any(s in jid for s in SPECIAL_JIDS)


def _mode(values: list[int]) -> int | None:
    return Counter(values).most_common(1)[0][0] if values else None


def learn(conn: sqlite3.Connection, ours: set[str]) -> tuple[dict, dict]:
    """Return (flags, profiles) learned only from rows WhatsApp wrote.

    flags[(type, from_me, is_group)] = ZFLAGS of downloaded media in that kind of chat.
    profiles[type] = TypeProfile.
    """
    rows = conn.execute(
        """
        SELECT m.ZMESSAGETYPE, m.ZISFROMME, s.ZSESSIONTYPE, s.ZCONTACTJID, m.ZFLAGS, m.ZSTANZAID,
               i.ZMEDIALOCALPATH, i.ZXMPPTHUMBPATH, i.ZVCARDNAME, i.ZVCARDSTRING, i.ZLATITUDE,
               m.ZMEDIASECTIONID, m.ZTEXT, i.ZTITLE, i.ZAUTHORNAME, i.ZMOVIEDURATION
        FROM ZWAMESSAGE m
        JOIN ZWAMEDIAITEM i ON i.Z_PK = m.ZMEDIAITEM
        JOIN ZWACHATSESSION s ON s.Z_PK = m.ZCHATSESSION
        WHERE m.ZMESSAGETYPE IN (1, 2, 3, 8, 11, 15)
        """
    ).fetchall()
    by_key: dict[tuple, list[int]] = {}
    profiles: dict[int, TypeProfile] = {}
    for (mtype, from_me, stype, jid, flags, stanza, local, thumb, sha, mime, lat,
         section, text, title, author, duration) in rows:
        if stanza and stanza in ours:
            continue  # written by an earlier wamover stage, not by WhatsApp
        if not local:
            continue  # only downloaded media shows what a working item looks like
        p = profiles.setdefault(mtype, TypeProfile())
        p.examples += 1
        p.uses_hash |= bool(sha)
        p.uses_mime |= bool(mime)
        p.uses_dims |= lat is not None and lat > 0
        p.uses_thumb |= bool(thumb)
        if thumb and mime:
            p.thumb_mimes.add(mime)
        p.sections[_section_style(section)] += 1
        p.title_used |= bool(title)
        if mtype == 8:
            p.name_in_author |= bool(author)
            p.uses_pages |= bool(duration)
            if text and author:
                p.text_rows += 1
                p.text_is_stem += int(text == author.rsplit(".", 1)[0] or text == author)
        if _special(jid) or stype not in (0, 1) or flags is None:
            continue
        by_key.setdefault((mtype, bool(from_me), stype == 1), []).append(flags)
    return {k: _mode(v) for k, v in by_key.items()}, profiles


def _section_style(value: str | None) -> str:
    if not value:
        return "none"
    m = re.fullmatch(r"(?:([a-z]+)-)?\d{4}-\d{2}", value)
    if not m:
        return "other"
    return f"{m.group(1)}-" if m.group(1) else "month"


def section_value(profile: TypeProfile, cocoa_date: float | None) -> str | None:
    """ZMEDIASECTIONID the way WhatsApp writes it for this type: '2026-11', 'doc-2026-11' or nothing."""
    style = profile.section_style
    if style in ("none", "other") or cocoa_date is None:
        return None
    month = _cocoa_section(cocoa_date)
    return month if style == "month" else f"{style}{month}"


def flags_for(flags: dict, mtype: int, from_me: bool, is_group: bool) -> int | None:
    """Only an exact match: same type, same direction, same kind of chat (1:1 or group).

    Flag bits differ between group and 1:1 photos on a real iPhone, so borrowing another
    kind's value would be a guess. A missing combination is skipped and reported instead.
    """
    return flags.get((mtype, from_me, is_group))


# ---------------------------------------------------------------------- files

def _sha256_b64(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return base64.b64encode(h.digest()).decode()


def _mime(path: Path, android_mime: str | None) -> str:
    return android_mime or mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def image_size(path: Path) -> tuple[int, int] | None:
    out = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
                         capture_output=True, text=True)
    nums = re.findall(r"pixel(?:Width|Height):\s*(\d+)", out.stdout)
    return (int(nums[0]), int(nums[1])) if out.returncode == 0 and len(nums) == 2 else None


def make_thumb(src: Path, dst: Path, kind: str) -> bool:
    """Small JPEG thumbnail with macOS's own tools (sips; qlmanage for video). No new dependencies.

    Never raises: a file that cannot get a thumbnail (timeout, damaged file) returns False.
    """
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        source = src
        with tempfile.TemporaryDirectory() as tmp:
            if kind == "video":
                with _VIDEO_SLOTS:
                    r = subprocess.run(["qlmanage", "-t", "-s", "320", "-o", tmp, str(src)],
                                       capture_output=True, text=True, timeout=VIDEO_THUMB_TIMEOUT)
                pngs = list(Path(tmp).glob("*.png"))
                if r.returncode != 0 or not pngs:
                    return False
                source = pngs[0]
            r = subprocess.run(
                ["sips", "-Z", str(THUMB_MAX), "-s", "format", "jpeg", "-s", "formatOptions", "70",
                 str(source), "--out", str(dst)],
                capture_output=True, text=True, timeout=THUMB_TIMEOUT,
            )
        return r.returncode == 0 and dst.is_file() and dst.read_bytes()[:2] == b"\xff\xd8"
    except (subprocess.TimeoutExpired, OSError):
        return False


def _local_path(session_jid: str, key_id: str, ext: str) -> str:
    u = str(uuid.uuid5(NAMESPACE, f"{session_jid}/{key_id}"))
    return f"Media/{session_jid}/{u[0]}/{u[1]}/{u}{ext}"


# ---------------------------------------------------------------------- media pass

def attach_media(db: Path, archive: Archive, media_root: Path, thumbs_dir: Path, out=print,
                 extra_files: dict[str, Path] | None = None,
                 page_counts: dict[str, int] | None = None,
                 fields_only: Archive | None = None) -> MediaReport:
    """Update ChatStorage in place (a copy inside the stage folder). Returns the files to put in the backup.

    Every media message that wamover imported is brought in line with how WhatsApp itself stores that
    type (gallery section label, document name fields, page count, sticker size), whether or not it
    has a file. Messages without a file get one if it is in the Media folder or in `extra_files`
    (found elsewhere, matched by SHA-256). Messages that already got a file in an earlier stage keep
    it; they only get their fields corrected and, for PDFs, a thumbnail if WhatsApp uses one.
    """
    report = MediaReport()
    resolver = MediaResolver(media_root)
    extra_files = extra_files or {}
    page_counts = page_counts or {}
    ours = {m.key_id for c in archive.chats for m in c.messages}
    wanted = {m.key_id: m for c in archive.chats for m in c.messages if m.media and m.kind in IOS_MEDIA_TYPE}
    # Messages left out of this import (e.g. never downloaded) may already be on the iPhone from an
    # earlier stage: correct their fields too, but never attach anything to them.
    extra_fields = {}
    if fields_only is not None:
        for c in fields_only.chats:
            for m in c.messages:
                if m.media and m.kind in IOS_MEDIA_TYPE and m.key_id not in wanted:
                    extra_fields[m.key_id] = m
                ours.add(m.key_id)
    primary = set(wanted)
    wanted.update(extra_fields)

    def source_for(key_id: str, message) -> Path | None:
        return resolver.resolve(message.media.android_path) or extra_files.get(key_id)

    conn = sqlite3.connect(db)
    try:
        flags, profiles = learn(conn, ours)
        out("Media types WhatsApp has shown on this iPhone: "
            + (", ".join(f"{TYPE_NAME.get(t, t)} ({p.examples})" for t, p in sorted(profiles.items())) or "none"))

        conn.execute("CREATE TEMP TABLE want (stanza TEXT PRIMARY KEY)")
        conn.executemany("INSERT OR IGNORE INTO want VALUES (?)", [(k,) for k in wanted])
        rows = conn.execute(
            """
            SELECT m.ZSTANZAID, m.Z_PK, m.ZMESSAGETYPE, m.ZISFROMME, m.ZMEDIAITEM,
                   s.ZCONTACTJID, s.ZSESSIONTYPE, i.ZMEDIALOCALPATH, i.ZXMPPTHUMBPATH, m.ZMESSAGEDATE,
                   i.ZVCARDSTRING
            FROM ZWAMESSAGE m
            JOIN want w ON w.stanza = m.ZSTANZAID
            JOIN ZWACHATSESSION s ON s.Z_PK = m.ZCHATSESSION
            LEFT JOIN ZWAMEDIAITEM i ON i.Z_PK = m.ZMEDIAITEM
            """
        ).fetchall()
        report.not_in_chatstorage = len(primary - {r[0] for r in rows})

        fixes: dict[int, tuple[int, dict, dict]] = {}   # item_pk -> (msg_pk, item values, message values)
        plan = []
        for stanza, msg_pk, mtype, from_me, item_pk, jid, stype, existing, thumb, date, old_mime in rows:
            if item_pk is None or not jid:
                report.not_in_chatstorage += 1
                continue
            message = wanted[stanza]
            profile = profiles.get(mtype)
            item_values: dict = {}
            msg_values: dict = {}
            if profile is not None:
                msg_values["ZMEDIASECTIONID"] = section_value(profile, date)
                if mtype == 8:
                    name = message.media.file_name or (message.media.android_path or "").rsplit("/", 1)[-1] or None
                    caption = (message.media.caption or "").strip() or None
                    if caption == name:
                        caption = None
                    if profile.name_in_author and name:
                        item_values["ZAUTHORNAME"] = name
                        if not profile.title_used:
                            item_values["ZTITLE"] = None
                    if caption:
                        msg_values["ZTEXT"] = caption
                    elif profile.text_from_name and name:
                        msg_values["ZTEXT"] = name.rsplit(".", 1)[0] if "." in name else name
                    if profile.uses_pages and page_counts.get(stanza):
                        item_values["ZMOVIEDURATION"] = int(page_counts[stanza])
            fixes[item_pk] = (msg_pk, item_values, msg_values)
            if stanza not in primary:
                continue

            if existing:
                report.already_had_file += 1
                known_mime = old_mime or message.media.mime_type or ""
                needs_thumb = (profile is not None and mtype == 8 and not thumb
                               and known_mime in profile.thumb_mimes)
                needs_dims = profile is not None and mtype == 15 and profile.uses_dims
                if needs_thumb or needs_dims:
                    src = source_for(stanza, message)
                    if src is not None:
                        plan.append(("fix", msg_pk, item_pk, mtype, None, profile, src, existing,
                                     known_mime if mtype == 8 else (old_mime or _mime(src, message.media.mime_type)),
                                     message))
                continue
            src = source_for(stanza, message)
            if src is None:
                report.no_file_on_android[mtype] += 1
                continue
            ref = flags_for(flags, mtype, bool(from_me), stype == 1)
            if profile is None or ref is None:
                report.no_reference[(mtype, bool(from_me), stype == 1)] += 1
                continue
            local = _local_path(jid, stanza, src.suffix.lower() or mimetypes.guess_extension(
                _mime(src, message.media.mime_type)) or "")
            plan.append(("new", msg_pk, item_pk, mtype, ref, profile, src, local,
                         _mime(src, message.media.mime_type), message))

        out(f"Preparing {len(plan)} media files (hashes and thumbnails) ...")

        def prepare(entry):
            mode, msg_pk, item_pk, mtype, ref, profile, src, local, mime, message = entry
            thumb_local = None
            wants_thumb = profile.uses_thumb and mtype in THUMB_KINDS and (
                mtype in THUMB_REQUIRED or mime in profile.thumb_mimes)
            if wants_thumb:
                candidate = local.rsplit(".", 1)[0] + ".thumb"
                if make_thumb(src, thumbs_dir / candidate, THUMB_KINDS[mtype]):
                    thumb_local = candidate
            dims = None
            if profile.uses_dims:
                if mtype == 15:
                    dims = image_size(src)   # Android stores 64x64 for many stickers; WhatsApp uses the real size
                elif message.media.width and message.media.height:
                    dims = (int(message.media.width), int(message.media.height))
                elif mtype == 1:
                    dims = image_size(src)
            sha = _sha256_b64(src) if (mode == "new" and profile.uses_hash) else None
            return entry, thumb_local, dims, sha, src.stat().st_size

        with ThreadPoolExecutor(max_workers=8) as pool:
            prepared = list(pool.map(prepare, plan))

        for entry, thumb_local, dims, sha, size in prepared:
            mode, msg_pk, item_pk, mtype, ref, profile, src, local, mime, message = entry
            _m, item_values, msg_values = fixes[item_pk]
            if dims:
                item_values["ZLONGITUDE"], item_values["ZLATITUDE"] = float(dims[0]), float(dims[1])
            if mode == "fix":
                if thumb_local:
                    item_values["ZXMPPTHUMBPATH"] = thumb_local
                    report.thumbs += 1
                    report.jobs.append({"relative_path": "Message/" + thumb_local,
                                        "local_file": str(thumbs_dir / thumb_local)})
                report.fixed[mtype] += 1
                continue
            if thumb_local is None and mtype in THUMB_REQUIRED and profile.uses_thumb:
                # WhatsApp always keeps a thumbnail for this type; without one, leave the bubble as it was.
                report.thumbs_failed += 1
                report.no_thumb[mtype] += 1
                continue
            item_values.update({"ZMEDIALOCALPATH": local, "ZFILESIZE": size})
            if sha:
                item_values["ZVCARDNAME"] = sha
            if profile.uses_mime:
                item_values["ZVCARDSTRING"] = mime
            if thumb_local:
                item_values["ZXMPPTHUMBPATH"] = thumb_local
                report.thumbs += 1
                report.jobs.append({"relative_path": "Message/" + thumb_local,
                                    "local_file": str(thumbs_dir / thumb_local)})
            msg_values["ZFLAGS"] = ref
            report.jobs.append({"relative_path": "Message/" + local, "local_file": str(src)})
            report.attached[mtype] += 1
            if src not in (resolver.resolve(message.media.android_path),):
                report.from_elsewhere[mtype] += 1
            report.bytes += size

        for item_pk, (msg_pk, item_values, msg_values) in fixes.items():
            if item_values:
                sets = ", ".join(f"{k}=?" for k in item_values)
                conn.execute(f"UPDATE ZWAMEDIAITEM SET {sets} WHERE Z_PK=?", [*item_values.values(), item_pk])
            if msg_values:
                sets = ", ".join(f"{k}=?" for k in msg_values)
                conn.execute(f"UPDATE ZWAMESSAGE SET {sets} WHERE Z_PK=?", [*msg_values.values(), msg_pk])
        report.fields_corrected = sum(1 for _m, iv, mv in fixes.values() if iv or mv)
        conn.commit()
    finally:
        conn.close()
    return report


def drop_undownloaded(archive: Archive, media_root: Path,
                      extra_files: dict[str, Path] | None = None) -> tuple[Archive, int]:
    """Leave out media messages whose file is not on the Mac and that have no caption.

    On the iPhone those would only be empty bubbles. Media messages with a caption are kept,
    because the caption is text someone wrote. Returns a new Archive and how many were left out.
    """
    from dataclasses import replace

    resolver = MediaResolver(media_root)
    dropped = 0
    chats = []
    for chat in archive.chats:
        kept = []
        for m in chat.messages:
            if m.media is not None and m.kind in IOS_MEDIA_TYPE and not (m.media.caption or "").strip() \
                    and resolver.resolve(m.media.android_path) is None and m.key_id not in (extra_files or {}):
                dropped += 1
                continue
            kept.append(m)
        if kept:
            chats.append(replace(chat, messages=kept))
    return Archive(owner_jid=archive.owner_jid, chats=chats, contacts=archive.contacts), dropped


# ---------------------------------------------------------------------- links

def _cocoa_section(cocoa: float) -> str:
    d = datetime.fromtimestamp(cocoa + COCOA_EPOCH_OFFSET, tz=timezone.utc)
    return f"{d.year}-{d.month:02d}"


def _clean(url: str) -> str:
    return url.rstrip(".,!?;:)]}*_~")


def add_links(db: Path, archive: Archive) -> int:
    """One WAMessageDataItem row per URL in imported messages, matching WhatsApp's own rows."""
    conn = sqlite3.connect(db)
    try:
        ent = conn.execute("SELECT Z_ENT, Z_MAX FROM Z_PRIMARYKEY WHERE Z_NAME='WAMessageDataItem'").fetchone()
        if ent is None:
            return 0
        sample = conn.execute(
            "SELECT ZCHATJID FROM ZWAMESSAGEDATAITEM WHERE ZTYPE=0 AND ZCONTENT1 LIKE 'http%' LIMIT 1").fetchone()
        if sample is None or not re.fullmatch(r"p\d+", sample[0] or ""):
            return 0  # no WhatsApp-made link row to copy the shape from
        z_ent, z_max = ent[0], ent[1] or 0
        conn.execute("CREATE TEMP TABLE ours (stanza TEXT PRIMARY KEY)")
        conn.executemany("INSERT OR IGNORE INTO ours VALUES (?)",
                         [(m.key_id,) for c in archive.chats for m in c.messages if m.text and URL_RE.search(m.text)])
        rows = conn.execute(
            """
            SELECT m.Z_PK, m.ZCHATSESSION, m.ZMESSAGEDATE, m.ZTEXT FROM ZWAMESSAGE m
            JOIN ours o ON o.stanza = m.ZSTANZAID
            WHERE m.ZTEXT IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM ZWAMESSAGEDATAITEM d WHERE d.ZMESSAGE = m.Z_PK)
            """
        ).fetchall()
        added = 0
        for msg_pk, session_pk, date, text in rows:
            for index, match in enumerate(URL_RE.finditer(text)):
                matched = _clean(match.group(0))
                if len(matched) < 8:
                    continue
                content = matched if matched.lower().startswith("http") else "http://" + matched
                z_max += 1
                conn.execute(
                    "INSERT INTO ZWAMESSAGEDATAITEM (Z_PK, Z_ENT, Z_OPT, ZINDEX, ZOWNSTHUMBNAIL, ZTYPE, ZMESSAGE, "
                    "ZDATE, ZCHATJID, ZCONTENT1, ZMATCHEDTEXT, ZSECTIONID) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (z_max, z_ent, 1, index, 0, 0, msg_pk, date, f"p{session_pk}", content, matched,
                     _cocoa_section(date or 0)),
                )
                added += 1
        conn.execute("UPDATE Z_PRIMARYKEY SET Z_MAX=? WHERE Z_NAME='WAMessageDataItem'", (z_max,))
        conn.commit()
        return added
    finally:
        conn.close()


def carry_archived(db: Path, archive: Archive) -> int:
    """Set ZARCHIVED on chats that were archived on Android. Never unarchives anything."""
    conn = sqlite3.connect(db)
    try:
        done = 0
        for chat in archive.chats:
            if not chat.archived:
                continue
            ids = [j for j in (chat.jid, chat.lid) if j]
            qs = ",".join("?" for _ in ids)
            cur = conn.execute(
                f"UPDATE ZWACHATSESSION SET ZARCHIVED=1 WHERE (ZARCHIVED IS NULL OR ZARCHIVED=0) AND "
                f"(ZCONTACTJID IN ({qs}) OR ZCONTACTIDENTIFIER IN ({qs}))", ids + ids)
            done += cur.rowcount
        conn.commit()
        return done
    finally:
        conn.close()


def media_bytes_needed(report: MediaReport) -> int:
    return sum(Path(j["local_file"]).stat().st_size for j in report.jobs)


def check_space(report: MediaReport, folder: Path) -> None:
    need = media_bytes_needed(report) * 1.2
    free = shutil.disk_usage(folder).free
    if free < need:
        raise MediaError(f"Media needs {need / 1e9:.1f} GB free in {folder}; only {free / 1e9:.1f} GB available.")
