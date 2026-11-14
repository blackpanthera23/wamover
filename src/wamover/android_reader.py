"""Read-only Android msgstore reader.

Parsing and the 2025+ LID-to-phone mapping come from WaBridge, which has been
run against a real msgstore.db. This module adds the one step that parser does
not: folding a LID chat and the phone-number chat for the same person into one.
"""

from __future__ import annotations

import json
from pathlib import Path

from wabridge.android import msgstore as msgstore_module
from wabridge.android.msgstore import MsgstoreError, load_vcf, parse
from wabridge.model import Archive, Chat as WaChat

from .models import Chat, Export, Media, Message


class UnsupportedSchemaError(RuntimeError):
    pass


def load_contacts(path: Path | None) -> dict[str, str]:
    """Phone digits -> name, from `wamover pull`'s contacts.json or a contacts .vcf export."""
    if path is None:
        return {}
    path = Path(path)
    if not path.is_file():
        raise UnsupportedSchemaError(f"Contacts file not found: {path}")
    if path.suffix.lower() == ".vcf":
        return load_vcf(str(path))
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(k): str(v) for k, v in data.items()}


def default_contacts(db: Path) -> Path | None:
    """contacts.json that `wamover pull` left next to the database, if any."""
    db = Path(db).resolve()
    for candidate in (db.parent / "contacts.json", db.parent / "android" / "contacts.json"):
        if candidate.is_file():
            return candidate
    return None


def _merge_same_jid(archive: Archive) -> Archive:
    """One person can have both a phone JID and a LID chat. Keep a single chat."""
    merged: dict[str, WaChat] = {}
    order: list[str] = []
    for chat in archive.chats:
        current = merged.get(chat.jid)
        if current is None:
            merged[chat.jid] = chat
            order.append(chat.jid)
            continue
        seen = {m.key_id for m in current.messages}
        for message in chat.messages:
            if message.key_id not in seen:
                current.messages.append(message)
                seen.add(message.key_id)
        current.messages.sort(key=lambda m: (m.timestamp_ms, m.key_id))
        if not current.name and chat.name:
            current.name = chat.name
        current.archived = current.archived or chat.archived
        known = {p.jid for p in current.participants}
        for participant in chat.participants:
            if participant.jid not in known:
                current.participants.append(participant)
                known.add(participant.jid)
    archive.chats = [merged[jid] for jid in order]
    return archive


def _tolerant_open(original):
    """Wrap WaBridge's open_db so one message with broken UTF-8 cannot stop the whole read.

    Real databases contain text with invalid byte sequences (usually damaged emoji).
    Undecodable bytes become U+FFFD instead of raising.
    """

    def open_db(path):
        conn = original(path)
        conn.text_factory = lambda raw: raw.decode("utf-8", "replace")
        return conn

    return open_db


def load_archive(path: Path, contacts: dict[str, str] | None = None) -> Archive:
    original = msgstore_module.open_db
    msgstore_module.open_db = _tolerant_open(original)
    try:
        archive = parse(str(path), contacts=contacts or {})
        # Unknown types with no text (polls, call logs, ...) become a bubble saying
        # "[Unsupported message type ...]". Leave them out; the HTML archive keeps them.
        for chat in archive.chats:
            chat.messages = [
                m for m in chat.messages
                if not (m.text and m.text.startswith("[Unsupported message type"))
            ]
        archive.chats = [c for c in archive.chats if c.messages]
    except MsgstoreError as exc:
        text = str(exc)
        if "legacy" in text or "pre-2022" in text:
            raise UnsupportedSchemaError(f"{text} (legacy)") from exc
        raise UnsupportedSchemaError(text) from exc
    finally:
        msgstore_module.open_db = original
    return _merge_same_jid(archive)


def read_android_db(path: Path, contacts: dict[str, str] | None = None) -> Export:
    archive = load_archive(path, contacts)
    warnings: list[str] = []
    chats: list[Chat] = []
    for index, chat in enumerate(archive.chats, start=1):
        if chat.jid.endswith("@lid"):
            warnings.append(
                f"{chat.jid} has no phone-number mapping, so it stays under its LID."
            )
        chats.append(
            Chat(
                id=index,
                jid=chat.jid,
                subject=chat.name,
                is_group=chat.is_group,
                messages=[
                    Message(
                        id=n,
                        chat_id=index,
                        from_me=m.from_me,
                        sender=m.sender_jid,
                        timestamp_ms=m.timestamp_ms,
                        text=m.text,
                        message_type=m.android_type or 0,
                        media=None
                        if m.media is None
                        else Media(
                            path=m.media.android_path,
                            mime_type=m.media.mime_type,
                            size=m.media.size,
                            name=m.media.file_name,
                        ),
                    )
                    for n, m in enumerate(chat.messages, start=1)
                ],
            )
        )
    return Export(chats=chats, warnings=warnings, names=dict(archive.contacts))
