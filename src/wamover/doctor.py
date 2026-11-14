"""Pre-flight checks. Reads only; installs nothing."""

from __future__ import annotations

import shutil
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from wabridge.android import adb

from . import ios_prepare
from .ios_backup_ro import is_encrypted
from .verify import Check

MIN_FREE_GB = 20


def _existing_parent(path: Path) -> Path:
    path = path.resolve()
    while not path.exists():
        path = path.parent
    return path


def run_doctor(work_dir: Path) -> list[Check]:
    checks: list[Check] = []

    try:
        checks.append(Check("wabridge engine installed", True, version("wabridge")))
    except PackageNotFoundError:
        checks.append(Check("wabridge engine installed", False, "Run `uv sync`."))

    try:
        exe = adb.find_adb()
        checks.append(Check("adb installed", True, exe))
        try:
            devices = adb.devices()
        except adb.AdbError as exc:
            checks.append(Check("Android phone over USB", False, str(exc), warn_only=True))
        else:
            ready = [d for d in devices if d.state == "device"]
            if ready:
                detail = ", ".join(d.model or d.serial for d in ready)
            elif devices:
                detail = f"state '{devices[0].state}'. Accept 'Allow USB debugging?' on the phone."
            else:
                detail = "None connected. Needed only for `wamover pull`."
            checks.append(Check("Android phone over USB", bool(ready), detail, warn_only=True))
    except adb.AdbError:
        checks.append(Check(
            "adb installed", False,
            "Needed only for `wamover pull`. Install with `brew install android-platform-tools`, "
            "or copy the files over USB yourself.",
            warn_only=True,
        ))

    store = ios_prepare.FINDER_BACKUP_ROOT / "Backup"
    if store.is_symlink() and not store.exists():
        checks.append(Check("Finder backup folder", False, f"{store} points to a drive that is not mounted."))
    else:
        try:
            entries = [p for p in store.iterdir() if (p / "Manifest.db").is_file()] if store.exists() else []
        except PermissionError:
            checks.append(Check(
                "Finder backup folder readable", False,
                "Give this app Full Disk Access (System Settings > Privacy & Security).",
            ))
        else:
            if not entries:
                checks.append(Check(
                    "Finder backup exists", False,
                    "None yet. Needed only for `wamover ios-prepare`.", warn_only=True,
                ))
            for p in entries:
                enc = is_encrypted(p)
                checks.append(Check(
                    f"Finder backup {p.name[:12]}... is unencrypted", not enc,
                    "Turn off 'Encrypt local backup' and back up again." if enc else str(p),
                    warn_only=True,
                ))

    import os
    from .verify import Check as _C
    perms = []
    for f in (Path("work/backup.key"), Path("work/msgstore.db")):
        if f.is_file():
            mode = os.stat(f).st_mode & 0o777
            perms.append(_C(f"{f} is not world-readable", mode & 0o077 == 0, oct(mode)))
    checks.extend(perms)

    free_gb = shutil.disk_usage(_existing_parent(Path(work_dir))).free / 1e9
    checks.append(Check(
        "Free disk space", free_gb >= MIN_FREE_GB, f"{free_gb:.0f} GB free. A full iPhone backup "
        "plus two copies needs roughly the iPhone's used storage (copies are free on APFS).",
        warn_only=True,
    ))
    return checks
