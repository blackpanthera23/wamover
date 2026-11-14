"""wamover command line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .android_pull import PullError, pull_android
from .android_reader import UnsupportedSchemaError, default_contacts, load_contacts, read_android_db
from .archive import ArchiveError, export_html, export_json
from .check_restore import LANDED_RATIO, check_restore
from .decrypt import DecryptError, decrypt_backup, detect_backup
from .doctor import run_doctor
from .install import InstallError, apply_install, plan_install
from .ios_backup_ro import BackupReadError
from .ios_prepare import PrepareError, stage_ios
from .safety import SourceModifiedError
from .recover import RecoverError
from .selection import Selection, SelectionError, parse_since, read_chat_list, write_plan
from .verify import Check, verify_work

ERRORS = (
    DecryptError, UnsupportedSchemaError, SourceModifiedError, PrepareError, PullError,
    ArchiveError, InstallError, BackupReadError, SelectionError, RecoverError,
)
STAGE = Path("work/stage1")


def _contacts_for(db: Path, explicit: Path | None) -> dict[str, str]:
    path = explicit or default_contacts(db)
    contacts = load_contacts(path)
    if contacts:
        print(f"Contact names from {path} ({len(contacts)} numbers)")
    return contacts


def _print_summary(export) -> None:
    print(f"Chats with messages: {len(export.chats)}")
    print(f"Total messages:      {export.message_count}")
    print(f"Messages with media: {export.media_count}")
    for c in export.chats[:20]:
        kind = "group" if c.is_group else "chat"
        print(f"  [{kind}] {c.title}: {len(c.messages)}")
    if len(export.chats) > 20:
        print(f"  ... and {len(export.chats) - 20} more")
    for w in export.warnings:
        print(f"warning: {w}")


def _print_checks(checks: list[Check]) -> bool:
    for c in checks:
        mark = "OK  " if c.ok else ("WARN" if c.warn_only else "FAIL")
        print(f"{mark}  {c.name}" + (f": {c.detail}" if c.detail else ""))
    return not any(c.blocking for c in checks)


def cmd_doctor(a) -> int:
    return 0 if _print_checks(run_doctor(a.work_dir)) else 1


def cmd_pull(a) -> int:
    summary = pull_android(a.dest, include_media=not a.no_media, include_contacts=not a.no_contacts,
                           serial=a.serial)
    print(f"Backup: {summary['crypt15']} ({summary['crypt15_bytes']:,} bytes)")
    print(f"Media folders: {', '.join(summary['media_dirs']) or 'none'}")
    print(f"Contact numbers: {summary['contacts']}")
    print("Nothing on the phone was changed.")
    return 0


def cmd_decrypt(a) -> int:
    info = detect_backup(a.backup)
    print(f"Detected crypt{info.crypt_version}, {info.size:,} bytes (source is read-only).")
    out = decrypt_backup(a.backup, a.key, a.work_dir, overwrite=a.overwrite)
    print(f"Decrypted to {out}")
    _print_summary(read_android_db(out, _contacts_for(out, None)))
    return 0


def cmd_inspect(a) -> int:
    _print_summary(read_android_db(a.db, _contacts_for(a.db, a.contacts)))
    return 0


def cmd_export(a) -> int:
    export = read_android_db(a.db, _contacts_for(a.db, a.contacts))
    out = Path(a.out)
    if not a.no_html:
        report: dict = {}
        print(f"HTML archive: {export_html(export, out / 'html', a.media, report)}")
        if a.media:
            print(f"Media linked: {report['media_linked']}, not found: {report['media_missing']}")
    print(f"JSON archive: {export_json(export, out / 'chats.json')}")
    return 0


def _selection(a) -> Selection:
    return Selection(
        since_ms=parse_since(a.since),
        jids=read_chat_list(a.chats) if a.chats else None,
    )


def cmd_plan(a) -> int:
    from .android_reader import load_archive

    since = parse_since(a.since)
    print("Reading the Android chats (several minutes for a large history) ...")
    archive = load_archive(a.db, _contacts_for(a.db, a.contacts))
    totals = write_plan(archive, a.out, since)
    print(f"Chat list: {a.out}")
    print(f"{totals['chats_total']} chats. Pre-selected (have messages in range): {totals['chats_selected']} chats, "
          f"{totals['messages_selected']:,} messages, {totals['media_selected']:,} with media.")
    print("Open it in Numbers or Excel, clear 'y' for chats you don't want, export as CSV, then pass it to")
    print(f"ios-prepare --chats {a.out}" + (f" --since {a.since}" if a.since else ""))
    return 0


def cmd_ios_prepare(a) -> int:
    live = stage_ios(a.db, a.backup, a.work_dir, _contacts_for(a.db, a.contacts), _selection(a),
                     media_root=a.media, skip_undownloaded=a.skip_undownloaded,
                     recovered=a.recovered, recovered_policy=a.recovered_policy, archived=a.archived,
                     cleanup_groups=a.cleanup_groups)
    print(f"Modified backup copy: {live}")
    print(f"Instructions: {a.work_dir / 'RESTORE.txt'}")
    print("Nothing was restored to the iPhone, and Finder's backup folder was not touched.")
    return 0


def cmd_find_files(a) -> int:
    """Search the Mac (and optionally the phone over adb) for missing attachments, by SHA-256. Read-only."""
    from .recover import collect, save, scan_mac, scan_phone, wanted_attachments

    home = Path.home()
    folders = a.folder or [
        home / "Library/Group Containers/group.net.whatsapp.WhatsApp.shared/Message/Media",
        home / "Downloads", home / "Documents", home / "Desktop", home / "Pictures",
    ]
    wanted = wanted_attachments(a.db, a.media)
    print(f"Attachments without a file: {sum(len(w.messages) for w in wanted.values())} "
          f"({len(wanted)} distinct files)")
    found = scan_mac([Path(f) for f in folders], wanted)
    if a.phone:
        found += scan_phone(wanted, serial=a.serial)
    out = Path(a.out)
    save(out / "matches.json", {"found": found})
    kept = collect(found, out / "files", serial=a.serial)
    save(out / "collected.json", {"files": kept})
    print(f"Found {len(found)} files; copied and re-checked {len(kept)} into {out / 'files'}")
    print(f"Use them with: ios-prepare ... --media {a.media} --recovered {out / 'collected.json'}")
    return 0


def cmd_verify(a) -> int:
    ok = _print_checks(verify_work(a.work_dir))
    print("Ready for install-backup." if ok else "Not ready. Fix the FAIL lines first.")
    return 0 if ok else 1


def cmd_install(a) -> int:
    plan = plan_install(a.work_dir, a.store)
    for line in plan.describe():
        print(line)
    if not a.apply:
        print("Dry run. Nothing was copied. Add --apply to do it.")
        return 0
    dest = apply_install(plan, a.work_dir)
    print(f"Installed and verified: {dest}")
    print("Next: turn off Find My iPhone, then Finder > iPhone > Restore Backup.")
    return 0


def cmd_check_restore(a) -> int:
    r = check_restore(a.backup, a.work_dir)
    print(f"Imported messages: {r.imported}")
    print(f"Found on the iPhone: {r.found} ({r.ratio:.0%})")
    print(f"ChatStorage on the iPhone: {r.phone_db_bytes:,} bytes (prepared: {r.prepared_db_bytes:,})")
    if r.landed:
        print("The restore landed.")
        return 0
    print(f"The restore did NOT land (needs at least {LANDED_RATIO:.0%}). The iPhone kept its old chats.")
    return 1


def _status(work_dir: Path, archive: Path, stage: Path | None = None) -> list[str]:
    stage = stage or work_dir / "stage1"
    crypt = work_dir / "android" / "Databases" / "msgstore.db.crypt15"
    db = work_dir / "msgstore.db"
    done, todo = [], []
    if crypt.is_file():
        done.append(f"Android backup copied: {crypt}")
    elif not db.is_file():
        todo.append(
            "On the Android phone: WhatsApp > Settings > Chats > Chat backup > End-to-end encrypted "
            "backup > use a 64-digit key. Save the key. Tap Back up. Do this BEFORE registering "
            "WhatsApp on the iPhone, because that logs the Android phone out."
        )
        todo.append(f"uv run wamover pull {work_dir / 'android'}   (USB debugging on)")
        return done + ["NEXT:"] + todo
    if db.is_file():
        done.append(f"Decrypted: {db}")
    else:
        todo.append(f'uv run wamover decrypt {crypt} --key "your 64-digit key" --work-dir {work_dir}')
        return done + ["NEXT:"] + todo
    if (archive / "html" / "index.html").is_file():
        done.append(f"Archive: {archive / 'html' / 'index.html'}")
    else:
        media = work_dir / "android" / "Media"
        todo.append(
            f"uv run wamover export {db} --out {archive}" + (f' --media "{media}"' if media.is_dir() else "")
        )
    if not (stage / "ios_backup").is_dir():
        todo.append(
            "iPhone: finish setup, install WhatsApp, register the SAME number, send one message. "
            "Finder > iPhone > untick 'Encrypt local backup' > Back Up Now."
        )
        todo.append(
            f"uv run wamover plan {db} --since 2026-01-01   (optional: review which chats go over)"
        )
        todo.append(
            f'uv run wamover ios-prepare {db} "$HOME/Library/Application Support/MobileSync/Backup/<UDID>" '
            f"--since 2026-01-01 --work-dir {stage}"
        )
        return done + ["NEXT:"] + todo
    done.append(f"Prepared backup: {stage / 'ios_backup'}")
    install = stage / "install.json"
    if not install.is_file():
        todo.append(f"uv run wamover verify --work-dir {stage}")
        todo.append(f"uv run wamover install-backup --work-dir {stage}   (then add --apply)")
        return done + ["NEXT:"] + todo
    info = json.loads(install.read_text(encoding="utf-8"))
    done.append(f"Installed into Finder: {info['installed']}")
    todo.append("Find My iPhone OFF, then Finder > iPhone > Restore Backup, then open WhatsApp.")
    todo.append(f'Finder > Back Up Now, then: uv run wamover check-restore "{info["installed"]}" --work-dir {stage}')
    return done + ["NEXT:"] + todo


def cmd_status(a) -> int:
    for line in _status(a.work_dir, a.archive, a.stage):
        print(line)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wamover", description="Android to iOS WhatsApp migrator")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status", help="Show what is done and the next command")
    s.add_argument("--work-dir", type=Path, default=Path("work"))
    s.add_argument("--archive", type=Path, default=Path("archive"))
    s.add_argument("--stage", type=Path, default=STAGE, help="The ios-prepare --work-dir to report on")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("doctor", help="Pre-flight checks (adb, Finder backups, disk space)")
    s.add_argument("--work-dir", type=Path, default=Path("work"))
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("pull", help="Copy the WhatsApp backup off the Android phone (read-only)")
    s.add_argument("dest", type=Path, nargs="?", default=Path("work/android"))
    s.add_argument("--no-media", action="store_true")
    s.add_argument("--no-contacts", action="store_true")
    s.add_argument("--serial", help="adb serial, if more than one phone is connected")
    s.set_defaults(func=cmd_pull)

    s = sub.add_parser("decrypt", help="Decrypt an Android backup (read-only on source)")
    s.add_argument("backup", type=Path, help="msgstore.db.crypt15 (or crypt12/14)")
    s.add_argument("--key", required=True, help="64-digit key (spaces ok) or key file path")
    s.add_argument("--work-dir", type=Path, default=Path("work"))
    s.add_argument("--overwrite", action="store_true")
    s.set_defaults(func=cmd_decrypt)

    for name, func, text in (
        ("inspect", cmd_inspect, "List chats in a decrypted msgstore.db"),
        ("export", cmd_export, "Export a readable HTML/JSON archive"),
    ):
        s = sub.add_parser(name, help=text)
        s.add_argument("db", type=Path)
        s.add_argument("--contacts", type=Path, help="contacts.json from pull, or a .vcf export")
        if name == "export":
            s.add_argument("--out", type=Path, default=Path("archive"))
            s.add_argument("--media", type=Path, help="The pulled Media folder, to include photos etc.")
            s.add_argument("--no-html", action="store_true")
        s.set_defaults(func=func)

    s = sub.add_parser("plan", help="List every chat in a CSV to choose what goes to the iPhone")
    s.add_argument("db", type=Path, help="Decrypted msgstore.db")
    s.add_argument("--since", help="YYYY-MM-DD; counts and pre-selection use only messages from this day")
    s.add_argument("--out", type=Path, default=Path("work/chats.csv"))
    s.add_argument("--contacts", type=Path)
    s.set_defaults(func=cmd_plan)

    s = sub.add_parser("ios-prepare", help="Write chats into a COPY of an unencrypted iPhone backup")
    s.add_argument("db", type=Path, help="Decrypted msgstore.db")
    s.add_argument("backup", type=Path, help="Folder that contains Manifest.db")
    s.add_argument("--work-dir", type=Path, default=STAGE, help="A new stage folder, e.g. work/stage1")
    s.add_argument("--since", help="YYYY-MM-DD; only messages from this day on")
    s.add_argument("--chats", type=Path, help="CSV from `wamover plan`; only rows with include=y")
    s.add_argument("--media", type=Path, help="Pulled Android Media folder, e.g. work/android/Media")
    s.add_argument("--skip-undownloaded", action="store_true",
                   help="Leave out media messages with no file on the Mac and no caption (needs --media)")
    s.add_argument("--recovered", type=Path, help="work/recovered/collected.json: files found elsewhere by SHA-256")
    s.add_argument("--recovered-policy", choices=("opened", "recent", "all"), default="opened",
                   help="opened: only files you opened in the last year (default); recent: also unopened files "
                        "changed in the last year; all: every found file")
    s.add_argument("--archived", action="store_true", help="Archive chats that were archived on Android")
    s.add_argument("--cleanup-groups", action="store_true",
                   help="Set inactive any group member rows WhatsApp did not write (after an older import)")
    s.add_argument("--contacts", type=Path)
    s.set_defaults(func=cmd_ios_prepare)

    s = sub.add_parser("find-files", help="Find missing attachments on the Mac/phone by WhatsApp's SHA-256")
    s.add_argument("db", type=Path, help="Decrypted msgstore.db")
    s.add_argument("--media", type=Path, default=Path("work/android/Media"))
    s.add_argument("--folder", type=Path, action="append",
                   help="Mac folder to search (repeatable). Default: WhatsApp Desktop, Downloads, Documents, "
                        "Desktop, Pictures")
    s.add_argument("--phone", action="store_true", help="Also search the Android phone over adb (read-only)")
    s.add_argument("--serial")
    s.add_argument("--out", type=Path, default=Path("work/recovered"))
    s.set_defaults(func=cmd_find_files)

    s = sub.add_parser("verify", help="Check the prepared backup before installing it")
    s.add_argument("--work-dir", type=Path, default=STAGE)
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("install-backup", help="Put the prepared backup in Finder's folder (dry run by default)")
    s.add_argument("--work-dir", type=Path, default=STAGE)
    s.add_argument("--store", type=Path, help="Override Finder's backup folder (for testing)")
    s.add_argument("--apply", action="store_true")
    s.set_defaults(func=cmd_install)

    s = sub.add_parser("check-restore", help="After restoring and backing up again, confirm the chats landed")
    s.add_argument("backup", type=Path, help="The new Finder backup folder")
    s.add_argument("--work-dir", type=Path, default=STAGE)
    s.set_defaults(func=cmd_check_restore)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ERRORS as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
