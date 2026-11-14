"""Put the prepared backup where Finder can restore it.

Dry run by default. With `apply`, an existing Finder backup for the same
iPhone is renamed to `<UDID>-<date>-<time>` (the name Finder uses for archived
backups), never deleted. The prepared copy is cloned in under a staging name
first and only renamed into place once the copy is complete.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import ios_prepare
from .ios_backup_ro import find_live_backup
from .verify import verify_backup, verify_work


class InstallError(RuntimeError):
    pass


@dataclass
class InstallPlan:
    source: Path
    store: Path
    dest: Path
    move_aside: Path | None
    staging: Path

    def describe(self) -> list[str]:
        lines = [f"Copy   {self.source}", f"  to   {self.dest}"]
        if self.move_aside:
            lines += [
                f"Rename the existing Finder backup {self.dest.name}",
                f"  to   {self.move_aside.name}  (kept, not deleted; this is your rollback)",
            ]
        lines.append(f"Staging folder during the copy: {self.staging.name}")
        return lines


def finder_store() -> Path:
    return ios_prepare.FINDER_BACKUP_ROOT / "Backup"


def _check_store(store: Path) -> None:
    if store.is_symlink() and not store.exists():
        raise InstallError(
            f"{store} points to {os.readlink(store)}, which is not available. "
            "Mount that drive first."
        )


def plan_install(work_dir: Path, store: Path | None = None) -> InstallPlan:
    store = Path(store) if store else finder_store()
    _check_store(store)
    source = find_live_backup(work_dir)
    blocking = [c for c in verify_work(work_dir) if c.blocking]
    if blocking:
        raise InstallError(
            "The prepared backup failed verification: "
            + "; ".join(f"{c.name} ({c.detail})" if c.detail else c.name for c in blocking)
        )
    dest = store / source.name
    move_aside = None
    if dest.exists():
        move_aside = store / f"{source.name}-{time.strftime('%Y%m%d-%H%M%S')}"
        if move_aside.exists():
            raise InstallError(f"{move_aside} already exists. Wait a second and retry.")
    staging = store / f".wamover-staging-{source.name}"
    if staging.exists():
        raise InstallError(
            f"{staging} is left over from an earlier attempt. wamover will not delete it. "
            "Move it away yourself, then retry."
        )
    return InstallPlan(source=source, store=store, dest=dest, move_aside=move_aside, staging=staging)


def _clone(src: Path, dst: Path) -> None:
    if platform.system() == "Darwin":
        result = subprocess.run(["cp", "-Rc", str(src), str(dst)], capture_output=True, text=True)
        if result.returncode == 0:
            return
        if dst.exists():
            raise InstallError(
                f"Copy failed and left a partial folder at {dst}. It was not deleted. "
                f"Details: {result.stderr.strip()}"
            )
    shutil.copytree(src, dst)


def apply_install(plan: InstallPlan, work_dir: Path) -> Path:
    try:
        plan.store.mkdir(parents=True, exist_ok=True)
        _clone(plan.source, plan.staging)
        if plan.move_aside:
            os.rename(plan.dest, plan.move_aside)
        try:
            os.rename(plan.staging, plan.dest)
        except OSError:
            if plan.move_aside and not plan.dest.exists():
                os.rename(plan.move_aside, plan.dest)
            raise
    except PermissionError as exc:
        raise InstallError(
            "macOS blocked access to Finder's backup folder. Give the app you run wamover from "
            "Full Disk Access (System Settings > Privacy & Security > Full Disk Access), then retry."
        ) from exc

    blocking = [c for c in verify_backup(plan.dest) if c.blocking]
    record = {
        "installed": str(plan.dest),
        "original_moved_to": str(plan.move_aside) if plan.move_aside else None,
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "verified": not blocking,
    }
    (Path(work_dir) / "install.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    if blocking:
        raise InstallError(
            "The copy in Finder's folder failed verification: " + "; ".join(c.name for c in blocking)
        )
    return plan.dest
