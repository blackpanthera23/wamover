"""Group membership: never write who is in a group. WhatsApp already knows.

What went wrong before (found on a real iPhone, Nov 2026): WaBridge copied Android's group participant
lists into ChatStorage as *active* members. Android lists you as the placeholder `lid_me`, and lists
everyone else by phone number, while WhatsApp on iOS lists members by LID. After the restore WhatsApp
compared those rows with its servers and posted "... left" notices in hundreds of groups, which also
pushed old chats to the top of the chat list.

Rules now:
- Android participant lists are not imported at all.
- A group message's sender is linked to WhatsApp's own member row when one exists (phone number
  mapped to LID through Android's jid_map). Only otherwise is a new row made, and it is inactive,
  the way WhatsApp keeps people who have left.
- `lid_me` and any other self placeholder never becomes a member.
- After the import, every member row wamover created is checked: none may be active.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path

from wabridge.model import Archive

SELF_PLACEHOLDERS = {"lid_me", "me", ""}


class GroupError(RuntimeError):
    pass


@dataclass
class GroupReport:
    participant_rows_not_imported: int = 0
    senders_linked_to_whatsapp_member: int = 0
    self_placeholders_dropped: int = 0
    new_member_rows: int = 0
    new_member_rows_made_inactive: int = 0

    def summary(self) -> dict:
        return self.__dict__.copy()


def _is_self(jid: str | None) -> bool:
    return jid is None or jid in SELF_PLACEHOLDERS or jid.startswith("lid_me")


def phone_to_lid(msgstore: Path) -> dict[str, str]:
    """Android jid_map: phone-number JID -> LID JID."""
    conn = sqlite3.connect(f"file:{Path(msgstore).resolve()}?mode=ro", uri=True)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "jid_map" not in tables:
            return {}
        return {p: lid for lid, p in conn.execute(
            "SELECT l.raw_string, p.raw_string FROM jid_map m "
            "JOIN jid l ON l._id = m.lid_row_id JOIN jid p ON p._id = m.jid_row_id")
            if lid and p and lid.endswith("@lid")}
    finally:
        conn.close()


def whatsapp_members(chatstorage: Path) -> dict[str, set[str]]:
    """group JID -> member JIDs that WhatsApp itself has for that group."""
    conn = sqlite3.connect(chatstorage)
    try:
        out: dict[str, set[str]] = {}
        for group, member in conn.execute(
            "SELECT s.ZCONTACTJID, g.ZMEMBERJID FROM ZWAGROUPMEMBER g "
            "JOIN ZWACHATSESSION s ON s.Z_PK = g.ZCHATSESSION WHERE s.ZSESSIONTYPE = 1"):
            if group and member:
                out.setdefault(group, set()).add(member)
        return out
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()


def prepare_groups(archive: Archive, msgstore: Path, chatstorage: Path) -> tuple[Archive, GroupReport]:
    """Return a copy of the archive that is safe to import into WhatsApp's groups."""
    report = GroupReport()
    to_lid = phone_to_lid(msgstore)
    existing = whatsapp_members(chatstorage)
    chats = []
    for chat in archive.chats:
        if not chat.is_group:
            chats.append(chat)
            continue
        report.participant_rows_not_imported += len(chat.participants)
        members = existing.get(chat.jid, set())
        messages = []
        for m in chat.messages:
            sender = m.sender_jid
            if not m.from_me and _is_self(sender):
                report.self_placeholders_dropped += int(sender is not None)
                sender = None
            elif sender and sender not in members and to_lid.get(sender) in members:
                sender = to_lid[sender]
                report.senders_linked_to_whatsapp_member += 1
            messages.append(m if sender == m.sender_jid else replace(m, sender_jid=sender))
        chats.append(replace(chat, participants=[], messages=messages))
    return Archive(owner_jid=archive.owner_jid, chats=chats, contacts=archive.contacts), report


def max_member_pk(chatstorage: Path) -> int:
    conn = sqlite3.connect(chatstorage)
    try:
        return conn.execute("SELECT COALESCE(MAX(Z_PK), 0) FROM ZWAGROUPMEMBER").fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


def check_new_members(chatstorage: Path, before_max: int, report: GroupReport) -> GroupReport:
    """Every member row created by this import must be inactive and must not be a self placeholder."""
    conn = sqlite3.connect(chatstorage)
    try:
        try:
            rows = conn.execute("SELECT Z_PK, ZMEMBERJID, ZISACTIVE FROM ZWAGROUPMEMBER WHERE Z_PK > ?",
                                (before_max,)).fetchall()
        except sqlite3.OperationalError:
            return report
        report.new_member_rows = len(rows)
        if any(_is_self(jid) for _pk, jid, _a in rows):
            raise GroupError("A self placeholder (lid_me) was about to be added as a group member.")
        active = [pk for pk, _j, a in rows if a]
        if active:
            conn.executemany("UPDATE ZWAGROUPMEMBER SET ZISACTIVE = 0 WHERE Z_PK = ?", [(pk,) for pk in active])
            conn.commit()
        report.new_member_rows_made_inactive = len(active)
        return report
    finally:
        conn.close()


def deactivate_foreign_members(chatstorage: Path) -> int:
    """Clean-up for an iPhone that an older wamover/WaBridge import already touched.

    WhatsApp on iOS keeps current group members by LID; its only phone-number member row is you,
    and that row is always inactive. So an *active* phone-number row, or any `lid_me` row, was not
    written by WhatsApp. Set those inactive. Skipped entirely if this WhatsApp build does not use
    LIDs for members (then phone numbers are its real format and must not be touched).
    """
    conn = sqlite3.connect(chatstorage)
    try:
        lids = conn.execute("SELECT COUNT(*) FROM ZWAGROUPMEMBER WHERE ZMEMBERJID LIKE '%@lid'").fetchone()[0]
        phones_active = conn.execute(
            "SELECT COUNT(*) FROM ZWAGROUPMEMBER WHERE ZISACTIVE = 1 AND ZMEMBERJID LIKE '%@s.whatsapp.net'"
        ).fetchone()[0]
        if lids == 0 or lids < phones_active:
            return 0
        cur = conn.execute(
            "UPDATE ZWAGROUPMEMBER SET ZISACTIVE = 0 WHERE ZISACTIVE = 1 AND "
            "(ZMEMBERJID LIKE '%@s.whatsapp.net' OR ZMEMBERJID LIKE 'lid_me%' OR ZMEMBERJID IN ('me', ''))")
        conn.commit()
        return cur.rowcount
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()
