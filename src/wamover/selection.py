"""Choose which chats and which time range go to the iPhone.

`write_plan` lists every chat in a CSV you can open in Numbers or Excel. Put
`y` in the `include` column for the chats you want, save as CSV, and pass the
file to `ios-prepare --chats`. `--since` keeps only messages on or after a date.
Both filters work together.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path

from wabridge.model import Archive

YES = {"y", "yes", "1", "x", "true"}
FIELDS = [
    "include", "title", "type", "messages_in_range", "media_in_range",
    "first_in_range", "last_message", "messages_total", "jid",
]


class SelectionError(RuntimeError):
    pass


@dataclass
class Selection:
    since_ms: int | None = None
    jids: set[str] | None = None

    def describe(self) -> str:
        parts = []
        if self.since_ms is not None:
            parts.append("since " + _fmt_day(self.since_ms))
        if self.jids is not None:
            parts.append(f"{len(self.jids)} chosen chats")
        return ", ".join(parts) or "everything"


def parse_since(text: str | None) -> int | None:
    """YYYY-MM-DD, local midnight, as Unix milliseconds."""
    if not text:
        return None
    try:
        day = date.fromisoformat(text)
    except ValueError as exc:
        raise SelectionError(f"--since must look like 2026-01-01, got {text!r}") from exc
    return int(datetime.combine(day, time.min).astimezone().timestamp() * 1000)


def _fmt_day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone().strftime("%Y-%m-%d")


def read_chat_list(path: Path) -> set[str]:
    path = Path(path)
    if not path.is_file():
        raise SelectionError(f"Chat list not found: {path}")
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames or "jid" not in reader.fieldnames or "include" not in reader.fieldnames:
            raise SelectionError(f"{path} needs 'include' and 'jid' columns. Start from `wamover plan`.")
        jids = {
            row["jid"].strip()
            for row in reader
            if row.get("jid") and (row.get("include") or "").strip().lower() in YES
        }
    if not jids:
        raise SelectionError(f"No chat in {path} has 'y' in the include column.")
    return jids


def apply_selection(archive: Archive, selection: Selection) -> Archive:
    """Return a new Archive holding only the selected chats and messages. The input is not changed."""
    chats = []
    for chat in archive.chats:
        if selection.jids is not None and chat.jid not in selection.jids:
            continue
        messages = chat.messages
        if selection.since_ms is not None:
            messages = [m for m in messages if m.timestamp_ms >= selection.since_ms]
        if not messages:
            continue
        chats.append(_with_messages(chat, messages))
    return Archive(owner_jid=archive.owner_jid, chats=chats, contacts=archive.contacts)


def _with_messages(chat, messages):
    from dataclasses import replace

    return replace(chat, messages=list(messages))


def write_plan(archive: Archive, out: Path, since_ms: int | None, preselect: bool = True) -> dict:
    out = Path(out)
    if out.exists():
        raise SelectionError(f"{out} already exists. Rename it or pick another file name.")
    rows = []
    for chat in archive.chats:
        in_range = [m for m in chat.messages if since_ms is None or m.timestamp_ms >= since_ms]
        rows.append({
            "include": "y" if (preselect and in_range) else "",
            "title": chat.name or chat.phone or chat.jid,
            "type": "group" if chat.is_group else "chat",
            "messages_in_range": len(in_range),
            "media_in_range": sum(1 for m in in_range if m.media),
            "first_in_range": _fmt_day(in_range[0].timestamp_ms) if in_range else "",
            "last_message": _fmt_day(chat.messages[-1].timestamp_ms) if chat.messages else "",
            "messages_total": len(chat.messages),
            "jid": chat.jid,
        })
    rows.sort(key=lambda r: (-r["messages_in_range"], -r["messages_total"]))
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    chosen = [r for r in rows if r["include"]]
    return {
        "chats_total": len(rows),
        "chats_selected": len(chosen),
        "messages_selected": sum(r["messages_in_range"] for r in chosen),
        "media_selected": sum(r["media_in_range"] for r in chosen),
    }
