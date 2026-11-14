"""Build a modified iPhone backup in a stage folder. Never restore it.

WaBridge writes the Android chats into ChatStorage.sqlite and numbers ZSORT
1..N. On a real iPhone that puts later messages above the imported history,
because WhatsApp keeps its own positive counter. The fix used in the Oct 2026
field report is to renumber every row to -N..-1. This module does that.

Safety rules:
- Finder's backup is only ever read (Manifest.db opened read-only, hashed before and after).
- Nothing is written under Finder's backup store.
- The two copies (pristine and live) are APFS clones, so they cost almost no disk space.
  If cloning is not possible, a real copy is made only when there is room for it.
"""

from __future__ import annotations

import dataclasses
import json
import platform
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

from wabridge.ios import backup as iosbackup
from wabridge.ios.backup import BackupError
from wabridge.ios.chatstorage import ChatStorageError, Writer
from wabridge.pipeline import PipelineError, Work
from wabridge.pipeline import inject as wb_inject

from .android_reader import load_archive
from .ios_backup_ro import BackupReadError, extract_chatstorage, is_encrypted, message_count
from .ios_backup_ro import entry as manifest_entry
from .ios_backup_ro import open_manifest
from .groups import GroupError, check_new_members, deactivate_foreign_members, max_member_pk, prepare_groups
from .media import MediaError, add_links, attach_media, carry_archived, check_space, drop_undownloaded
from .recover import RecoverError, page_counts, recovered_for_messages
from .safety import assert_unchanged, sha256_file
from .selection import Selection, apply_selection

FINDER_BACKUP_ROOT = Path.home() / "Library/Application Support/MobileSync"
SPACE_MARGIN = 1.1
NOTICE_TYPES = "(6, 10)"  # WhatsApp's own notices; never what a chat is sorted by
# Directory mode WhatsApp itself uses in the backup (WaBridge writes 0o40775).
WHATSAPP_DIR_MODE = 0o40755


class PrepareError(RuntimeError):
    pass


def _under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def assert_not_finder_store(path: Path) -> None:
    if _under(path, FINDER_BACKUP_ROOT):
        raise PrepareError(
            f"Refusing to write under Finder's backup store ({FINDER_BACKUP_ROOT}). "
            "wamover only writes into its own folders. `install-backup` is the one step that "
            "puts a backup there, and only with --apply."
        )


def _tree_bytes(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def clone_tree(src: Path, dest: Path) -> str:
    """Copy src to dest. Returns 'clone' or 'copy'. Never overwrites, never deletes."""
    if dest.exists():
        raise PrepareError(f"{dest} already exists. wamover will not overwrite it. Use a new --work-dir.")
    if src.resolve() == dest.resolve():
        raise PrepareError(f"Refusing to copy {src} onto itself.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if platform.system() == "Darwin":
        result = subprocess.run(["cp", "-Rc", str(src), str(dest)], capture_output=True, text=True)
        if result.returncode == 0:
            return "clone"
        if dest.exists():
            raise PrepareError(
                f"Cloning {src} failed part-way and left {dest}. It was not deleted; move it away "
                f"yourself before retrying. Details: {result.stderr.strip()}"
            )
    need = _tree_bytes(src) * SPACE_MARGIN
    free = shutil.disk_usage(dest.parent).free
    if free < need:
        raise PrepareError(
            f"Not enough disk space for a full copy of the iPhone backup: need {need / 1e9:.1f} GB, "
            f"have {free / 1e9:.1f} GB. Put --work-dir on the same APFS disk as the backup, or free space."
        )
    shutil.copytree(src, dest)
    return "copy"


class _CloningShutil:
    """Stand-in for the `shutil` name inside WaBridge's backup module: copyfile clones on APFS.

    Only WaBridge's module sees this object, and only while inject runs. A failed clone (other
    file system, or the destination already exists) falls back to a normal copy.
    """

    def __init__(self, real):
        self._real = real
        self._clonefile = None
        if platform.system() == "Darwin":
            import ctypes
            import ctypes.util

            libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            fn = libc.clonefile
            fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32]
            fn.restype = ctypes.c_int
            self._clonefile = fn
        self.cloned = 0
        self.copied = 0

    def copyfile(self, src, dst, *args, **kwargs):
        if self._clonefile is not None and not Path(dst).exists():
            if self._clonefile(str(src).encode(), str(dst).encode(), 0) == 0:
                self.cloned += 1
                return dst
        self.copied += 1
        return self._real.copyfile(src, dst, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def apply_negative_zsort(db: Path) -> int:
    """Renumber ZSORT to -N..-1 in date order. Returns the number of messages."""
    conn = sqlite3.connect(db)
    try:
        n = conn.execute("SELECT COUNT(*) FROM ZWAMESSAGE").fetchone()[0]
        if not n:
            return 0
        conn.execute(
            """
            WITH ranked AS (
                SELECT Z_PK,
                       ROW_NUMBER() OVER (ORDER BY ZMESSAGEDATE, Z_PK) AS rn,
                       COUNT(*) OVER () AS n
                FROM ZWAMESSAGE
            )
            UPDATE ZWAMESSAGE
            SET ZSORT = (
                SELECT rn - n - 1 FROM ranked WHERE ranked.Z_PK = ZWAMESSAGE.Z_PK
            )
            """
        )
        session_cols = {r[1] for r in conn.execute("PRAGMA table_info(ZWACHATSESSION)")}
        if {"ZLASTMESSAGE", "ZLASTMESSAGEDATE"} <= session_cols:
            # The chat list sorts by ZLASTMESSAGEDATE. WhatsApp's own notices (group events, security
            # and encryption notices: types 6 and 10) are written with the date of the restore, which
            # would put years-old chats at the top. Use the newest real message; only a chat with
            # nothing but notices keeps its newest notice.
            conn.execute(
                f"""
                WITH pick AS (
                    SELECT s.Z_PK AS sid, COALESCE(
                        (SELECT m.Z_PK FROM ZWAMESSAGE m WHERE m.ZCHATSESSION = s.Z_PK
                           AND m.ZMESSAGETYPE NOT IN {NOTICE_TYPES} ORDER BY m.ZSORT DESC LIMIT 1),
                        (SELECT m.Z_PK FROM ZWAMESSAGE m WHERE m.ZCHATSESSION = s.Z_PK
                           ORDER BY m.ZSORT DESC LIMIT 1)) AS mid
                    FROM ZWACHATSESSION s
                    WHERE s.Z_PK IN (SELECT DISTINCT ZCHATSESSION FROM ZWAMESSAGE)
                )
                UPDATE ZWACHATSESSION SET
                    ZLASTMESSAGE = (SELECT mid FROM pick WHERE pick.sid = ZWACHATSESSION.Z_PK),
                    ZLASTMESSAGEDATE = (SELECT m.ZMESSAGEDATE FROM pick JOIN ZWAMESSAGE m ON m.Z_PK = pick.mid
                                        WHERE pick.sid = ZWACHATSESSION.Z_PK)
                WHERE Z_PK IN (SELECT sid FROM pick)
                """
            )
        check = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise PrepareError(f"ChatStorage failed integrity_check: {check}")
        conn.commit()
        conn.execute("PRAGMA journal_mode=DELETE")
        sorts = [r[0] for r in conn.execute("SELECT ZSORT FROM ZWAMESSAGE ORDER BY ZMESSAGEDATE, Z_PK")]
        if sorts != list(range(-n, 0)):
            raise PrepareError(f"ZSORT fix did not land on {-n}..-1.")
        return n
    finally:
        conn.close()


def _checkpoint_manifest(folder: Path) -> None:
    conn = sqlite3.connect(folder / "Manifest.db")
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.commit()
    finally:
        conn.close()


def _check_source(backup_src: Path) -> None:
    """Read-only checks on Finder's backup."""
    if is_encrypted(backup_src):
        raise PrepareError(
            f"{backup_src} is encrypted. Finder > iPhone > untick 'Encrypt local backup', back up again."
        )
    try:
        conn = open_manifest(backup_src)
    except BackupReadError as exc:
        raise PrepareError(str(exc)) from exc
    except sqlite3.OperationalError as exc:
        raise PrepareError(
            f"Cannot read {backup_src / 'Manifest.db'} ({exc}). Give Kiro Full Disk Access "
            "(System Settings > Privacy & Security > Full Disk Access), then restart it."
        ) from exc
    try:
        found = manifest_entry(conn, "ChatStorage.sqlite") is not None
    finally:
        conn.close()
    if not found:
        raise PrepareError(
            "WhatsApp's ChatStorage.sqlite is not in this backup. Install WhatsApp on the iPhone, "
            "register the same number, send one message, and back up again."
        )


def _write_instructions(work_dir: Path, live: Path, messages: int, selection: Selection) -> None:
    text = f"""\
This folder is a COPY. Nothing has been restored to the iPhone.

Modified backup: {live}
Migrated: {selection.describe()}
Messages in ChatStorage after the sort fix: {messages}

Next steps:

1. uv run wamover verify --work-dir {work_dir}
2. uv run wamover install-backup --work-dir {work_dir}            (dry run, shows the plan)
   uv run wamover install-backup --work-dir {work_dir} --apply    (copies it into Finder's folder;
   an existing backup for this iPhone is renamed, never deleted)
3. iPhone: Settings > [your name] > Find My > Find My iPhone OFF.
4. Finder > iPhone > General > Restore Backup. Pick the newest backup. Keep the cable in
   until the iPhone shows the lock screen again. Then turn Find My back on.
5. Open WhatsApp on the iPhone.
6. Finder > Back Up Now, then:
   uv run wamover check-restore "<Finder backup folder>" --work-dir {work_dir}
   A restore can report success without changing anything; this check tells you.

Rollback: the renamed original backup (step 2) is still in Finder's list. The untouched
copy is also at {work_dir / "ios_backup_pristine"}. The Android backup is untouched.
"""
    (work_dir / "RESTORE.txt").write_text(text, encoding="utf-8")


def stage_ios(
    msgstore_db: Path,
    backup_src: Path,
    work_dir: Path,
    contacts: dict[str, str] | None = None,
    selection: Selection | None = None,
    out=print,
    media_root: Path | None = None,
    skip_undownloaded: bool = False,
    recovered: Path | None = None,
    recovered_policy: str = "opened",
    archived: bool = False,
    cleanup_groups: bool = False,
) -> Path:
    """Copy `backup_src`, write the selected Android chats into the copy, and stop there."""
    selection = selection or Selection()
    msgstore_db = Path(msgstore_db).resolve()
    backup_src = Path(backup_src).resolve()
    work_dir = Path(work_dir).resolve()
    assert_not_finder_store(work_dir)
    if not msgstore_db.is_file():
        raise PrepareError(f"Decrypted database not found: {msgstore_db}")
    if media_root is not None and not Path(media_root).is_dir():
        raise PrepareError(f"Media folder not found: {media_root}")
    if skip_undownloaded and media_root is None:
        raise PrepareError("--skip-undownloaded needs --media, to know which files exist.")
    if recovered is not None and media_root is None:
        raise PrepareError("--recovered needs --media.")
    extra_files: dict = {}
    recovered_counts = None
    if recovered is not None:
        try:
            extra_files, recovered_counts = recovered_for_messages(
                Path(recovered), msgstore_db, Path(media_root), recovered_policy)
        except (RecoverError, OSError, KeyError, ValueError) as exc:
            raise PrepareError(f"Cannot use {recovered}: {exc}") from exc
        out(f"Found files used ({recovered_policy}): {recovered_counts['files_used']} files for "
            f"{recovered_counts['messages']} messages; {recovered_counts['files_skipped_not_opened']} not opened "
            "in the last year, left out.")
    if not (backup_src / "Manifest.db").is_file():
        raise PrepareError(f"No Manifest.db in {backup_src}. Pass the backup folder, not a file inside it.")
    if _under(backup_src, work_dir):
        raise PrepareError("The backup source is inside the stage folder. Pick a new --work-dir.")
    if (work_dir / "convert").exists() or (work_dir / "ios_backup").exists():
        raise PrepareError(f"{work_dir} already holds a prepared backup. Use a new --work-dir.")

    _check_source(backup_src)
    manifest = backup_src / "Manifest.db"
    before = sha256_file(manifest)

    # Read and filter the Android side first: it is slow, and a mistake here should not
    # leave half-made iPhone copies behind.
    out("Reading the Android chats (several minutes for a large history) ...")
    archive = apply_selection(load_archive(msgstore_db, contacts), selection)
    selected_archive = archive
    undownloaded_left_out = 0
    if skip_undownloaded:
        archive, undownloaded_left_out = drop_undownloaded(archive, Path(media_root), extra_files)
        out(f"Left out {undownloaded_left_out} photos/videos/etc. that were never downloaded and have no caption.")
    selected = sum(len(c.messages) for c in archive.chats)
    if not selected:
        raise PrepareError(f"Nothing to migrate for {selection.describe()}.")
    out(f"Selected {len(archive.chats)} chats, {selected} messages ({selection.describe()}).")

    name = backup_src.name
    live = work_dir / "ios_backup" / name
    pristine = work_dir / "ios_backup_pristine" / name
    out("Cloning the iPhone backup ...")
    methods = {"pristine": clone_tree(backup_src, pristine), "live": clone_tree(backup_src, live)}
    assert_unchanged(manifest, before)

    out_db = work_dir / "convert" / "ChatStorage.sqlite"
    try:
        extract_chatstorage(pristine, out_db.parent)
    except BackupReadError as exc:
        raise PrepareError(str(exc)) from exc
    before_count = message_count(out_db)

    archive, group_report = prepare_groups(archive, msgstore_db, out_db)
    members_before = max_member_pk(out_db)

    out("Writing the chats into ChatStorage ...")
    try:
        writer = Writer(str(out_db))
        report = writer.import_archive(archive, media_root=None, include_media=False)
        writer.finalize()
        check_new_members(out_db, members_before, group_report)
    except (ChatStorageError, GroupError) as exc:
        raise PrepareError(str(exc)) from exc
    out(f"Groups: {group_report.participant_rows_not_imported} Android participant rows not imported, "
        f"{group_report.senders_linked_to_whatsapp_member} senders linked to WhatsApp's own member list, "
        f"{group_report.new_member_rows} new (inactive) member rows.")

    media_summary = None
    jobs: list[dict] = []
    links = add_links(out_db, archive)
    out(f"Links listed for the Links tab: {links}")
    if media_root is not None:
        try:
            media = attach_media(out_db, archive, Path(media_root), work_dir / "convert" / "thumbs", out=out,
                                 extra_files=extra_files, page_counts=page_counts(msgstore_db),
                                 fields_only=selected_archive)
            check_space(media, work_dir)
        except MediaError as exc:
            raise PrepareError(str(exc)) from exc
        media.links_added = links
        media_summary = media.summary()
        jobs = media.jobs
        out("Media: " + json.dumps(media_summary["attached"], ensure_ascii=False)
            + f", thumbnails {media.thumbs}, skipped (no example on the iPhone) "
            + json.dumps(media_summary["skipped_no_iphone_example"], ensure_ascii=False))

    members_cleaned = deactivate_foreign_members(out_db) if cleanup_groups else 0
    if cleanup_groups:
        out(f"Group member rows not written by WhatsApp, set inactive: {members_cleaned}")
    archived_set = carry_archived(out_db, archive) if archived else 0
    if archived:
        out(f"Chats set to archived, as on Android: {archived_set}")

    messages = apply_negative_zsort(out_db)
    if messages != before_count + report.messages_written:
        raise PrepareError(
            f"ChatStorage has {messages} messages; expected {before_count} + {report.messages_written}."
        )
    payload = dataclasses.asdict(report)
    payload["media_jobs"] = jobs
    (work_dir / "convert" / "report.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    out("Putting ChatStorage into the copied backup ...")
    original_dir_mode, original_shutil = iosbackup.MODE_DIR, iosbackup.shutil
    cloner = _CloningShutil(shutil)
    iosbackup.MODE_DIR, iosbackup.shutil = WHATSAPP_DIR_MODE, cloner
    try:
        wb_inject(Work(str(work_dir)), backup_folder=str(live), out=lambda _l: None)
    except (PipelineError, BackupError) as exc:
        raise PrepareError(str(exc)) from exc
    finally:
        iosbackup.MODE_DIR, iosbackup.shutil = original_dir_mode, original_shutil
    methods["files_into_backup"] = {"cloned": cloner.cloned, "copied": cloner.copied}
    _checkpoint_manifest(live)
    assert_unchanged(manifest, before)

    (work_dir / "stage.json").write_text(json.dumps({
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source_backup": str(backup_src),
        "source_manifest_sha256": before,
        "msgstore": str(msgstore_db),
        "selection": selection.describe(),
        "since_ms": selection.since_ms,
        "chats_selected": len(archive.chats),
        "messages_selected": selected,
        "messages_written": report.messages_written,
        "messages_already_on_iphone": report.messages_skipped_duplicate,
        "messages_before": before_count,
        "messages_after": messages,
        "copy_method": methods,
        "links_added": links,
        "undownloaded_left_out": undownloaded_left_out,
        "found_files": recovered_counts,
        "recovered_policy": recovered_policy if recovered is not None else None,
        "archived_set": archived_set,
        "groups": {**group_report.summary(), "foreign_members_set_inactive": members_cleaned},
        "media": media_summary,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    _write_instructions(work_dir, live, messages, selection)
    return live
