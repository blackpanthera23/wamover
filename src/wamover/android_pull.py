"""Copy the WhatsApp backup off the Android phone over USB (adb).

Only read commands run on the phone: `adb pull`, `ls`, `du`, and a contacts
query. Nothing on the phone is changed. The local folder must be new or empty,
so an earlier pull is never overwritten.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path

from wabridge.android import adb

from .safety import sha256_file


class PullError(RuntimeError):
    pass


def pull_android(
    dest: Path,
    *,
    include_media: bool = True,
    include_contacts: bool = True,
    serial: str | None = None,
    out: Callable[[str], None] = print,
) -> dict:
    dest = Path(dest)
    if dest.exists() and any(dest.iterdir()):
        raise PullError(f"{dest} is not empty. Pick a new folder; wamover does not overwrite earlier pulls.")
    try:
        device = adb.require_device(serial)
        root = adb.detect_wa_root(device.serial)
        out(f"Phone: {device.model or device.serial}")
        out(f"WhatsApp folder: {root}")
        dest.mkdir(parents=True, exist_ok=True)
        out("Copying msgstore.db.crypt15 ...")
        adb.pull_databases(str(dest), root, serial=device.serial)
        media_dirs: list[str] = []
        if include_media:
            out("Copying media. This can take a long time for a big chat history ...")
            media_dirs = adb.pull_media(str(dest), root, serial=device.serial)
        contacts = adb.export_contacts(device.serial) if include_contacts else {}
    except adb.AdbError as exc:
        raise PullError(str(exc)) from exc

    crypt = dest / "Databases" / "msgstore.db.crypt15"
    if not crypt.is_file():
        raise PullError(f"adb finished but {crypt} is missing.")
    if contacts:
        (dest / "contacts.json").write_text(json.dumps(contacts, ensure_ascii=False), encoding="utf-8")
    summary = {
        "phone": device.model or device.serial,
        "whatsapp_root": root,
        "crypt15": str(crypt),
        "crypt15_sha256": sha256_file(crypt),
        "crypt15_bytes": crypt.stat().st_size,
        "media_dirs": media_dirs,
        "contacts": len(contacts),
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    (dest / "pull.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary
