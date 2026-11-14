"""Readable HTML/JSON archive. Independent of the iPhone, so it is the safety net.

With a media folder, photos, videos, voice notes and documents are linked into
the archive. Files are hard-linked when possible (no extra disk space; the
original is never modified) and copied otherwise.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from .models import Chat, Export, Message


class ArchiveError(RuntimeError):
    pass


def _fmt_ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")


def _slug(text: str, fallback: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")
    return s[:60] or fallback


class MediaResolver:
    """Maps an Android path such as `Media/WhatsApp Images/IMG-1.jpg` to a local file."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise ArchiveError(f"Media folder not found: {root}")
        self._index: dict[str, Path] | None = None

    def _by_name(self) -> dict[str, Path]:
        if self._index is None:
            self._index = {}
            for dirpath, _dirs, files in os.walk(self.root):
                for name in files:
                    self._index.setdefault(name, Path(dirpath) / name)
        return self._index

    def resolve(self, android_path: str | None) -> Path | None:
        if not android_path:
            return None
        rel = android_path.lstrip("/")
        if rel.startswith("Media/"):
            rel = rel[len("Media/"):]
        candidate = (self.root / rel).resolve()
        # A path from the database must not reach outside the media folder.
        if candidate.is_relative_to(self.root) and candidate.is_file():
            return candidate
        return self._by_name().get(Path(rel).name)


def _place(src: Path, media_out: Path) -> str:
    tag = hashlib.sha1(str(src).encode()).hexdigest()[:8]
    dst = media_out / f"{tag}-{src.name}"
    if not dst.exists():
        try:
            os.link(src, dst)
        except OSError:
            shutil.copy2(src, dst)
    return f"media/{dst.name}"


def _media_html(message: Message, href: str | None) -> str:
    media = message.media
    label = html.escape(media.name or (media.path or "").rsplit("/", 1)[-1] or media.mime_type or "file")
    if href is None:
        return f" <em>[media not found: {label}]</em>"
    url = html.escape(quote(href))
    mime = (media.mime_type or "").lower()
    if mime.startswith("image/"):
        return f'<br><a href="{url}"><img src="{url}" loading="lazy" alt="{label}"></a>'
    if mime.startswith("video/"):
        return f'<br><video src="{url}" controls preload="none"></video>'
    if mime.startswith("audio/"):
        return f'<br><audio src="{url}" controls preload="none"></audio>'
    return f' <a href="{url}">{label}</a>'


def export_json(export: Export, out_file: Path) -> Path:
    """One JSON array of chats, written chat by chat so a large history never sits in memory twice."""
    if out_file.exists():
        raise ArchiveError(f"{out_file} already exists. Choose a new --out folder.")
    out_file.parent.mkdir(parents=True, exist_ok=True)
    partial = out_file.with_name(out_file.name + ".partial")
    with open(partial, "x", encoding="utf-8") as fh:
        fh.write("[\n")
        for i, c in enumerate(export.chats):
            chat = {
                "jid": c.jid, "subject": c.subject, "is_group": c.is_group,
                "messages": [
                    {
                        "id": m.id, "from_me": m.from_me, "sender": m.sender,
                        "sender_name": export.names.get(m.sender) if m.sender else None,
                        "timestamp_ms": m.timestamp_ms, "text": m.text, "type": m.message_type,
                        "media": None if m.media is None else {
                            "path": m.media.path, "mime_type": m.media.mime_type,
                            "size": m.media.size,
                        },
                    }
                    for m in c.messages
                ],
            }
            fh.write((",\n" if i else "") + json.dumps(chat, ensure_ascii=False))
        fh.write("\n]\n")
    partial.rename(out_file)
    return out_file


def _chat_html(chat: Chat, export: Export, resolver: MediaResolver | None, media_out: Path,
               report: dict) -> str:
    rows = []
    for m in chat.messages:
        if m.from_me:
            who = "Me"
        elif m.sender:
            who = export.names.get(m.sender, m.sender)
        else:
            who = chat.title
        body = html.escape(m.text or "")
        if m.media:
            href = None
            if resolver:
                src = resolver.resolve(m.media.path)
                if src:
                    href = _place(src, media_out)
            report["media_linked" if href else "media_missing"] += 1
            body += _media_html(m, href)
        cls = "me" if m.from_me else "them"
        rows.append(
            f'<div class="m {cls}"><b>{html.escape(who)}</b> '
            f'<small>{_fmt_ts(m.timestamp_ms)}</small><p>{body}</p></div>'
        )
    return (
        "<!doctype html><meta charset='utf-8'>"
        f"<title>{html.escape(chat.title)}</title>"
        "<style>body{font-family:sans-serif;max-width:700px;margin:2em auto}"
        ".m{margin:.5em 0;padding:.5em;border-radius:6px}.me{background:#dcf8c6}"
        ".them{background:#eee}p{margin:.2em 0;white-space:pre-wrap}"
        "img,video{max-width:100%;max-height:360px;border-radius:4px}</style>"
        f"<h1>{html.escape(chat.title)}</h1>" + "".join(rows)
    )


def export_html(export: Export, out_dir: Path, media_root: Path | None = None,
                report: dict | None = None) -> Path:
    index = out_dir / "index.html"
    if index.exists():
        raise ArchiveError(f"{index} already exists. Choose a new --out folder.")
    resolver = MediaResolver(media_root) if media_root else None
    report = report if report is not None else {}
    report.setdefault("media_linked", 0)
    report.setdefault("media_missing", 0)
    out_dir.mkdir(parents=True, exist_ok=True)
    media_out = out_dir / "media"
    if resolver:
        media_out.mkdir(exist_ok=True)
    links = []
    for i, chat in enumerate(export.chats):
        name = f"{i:04d}_{_slug(chat.title, 'chat')}.html"
        (out_dir / name).write_text(_chat_html(chat, export, resolver, media_out, report), encoding="utf-8")
        links.append(
            f'<li><a href="{html.escape(name)}">{html.escape(chat.title)}</a> '
            f"({len(chat.messages)})</li>"
        )
    index.write_text(
        "<!doctype html><meta charset='utf-8'><title>WhatsApp archive</title>"
        "<h1>WhatsApp archive</h1><ul>" + "".join(links) + "</ul>",
        encoding="utf-8",
    )
    return index
