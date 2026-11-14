# wamover

Move WhatsApp history from an Android phone to an iPhone that is already set up, on a Mac, without paid tools.

Tested end to end on real phones in November 2026 (Pixel 8 Pro, Android → iPhone, iOS 27.2.1, WhatsApp 26.x): 2.69 million messages from 2013 to 2026, 39,903 media files and 171,233 links. Read [Known risks](#known-risks) before you use it.

## How it works
1. Copy WhatsApp's end-to-end encrypted local backup (`msgstore.db.crypt15`) and its media folder off the Android phone (adb, read-only).
2. Decrypt it on the Mac with your 64-digit key. Decryption runs in-process and in chunks, so multi-GB backups work and the key never appears on a command line.
3. Take an unencrypted Finder backup of the iPhone, with WhatsApp already registered on the same number.
4. Write the Android chats into a **copy** of that backup's `ChatStorage.sqlite`, using [WaBridge](https://github.com/Parv3sh/wabridge) as the conversion engine, plus the fixes below.
5. `verify`, then put the copy in Finder's backup folder (the old backup is renamed, never deleted) and restore it with Finder.
6. Take another backup and run `check-restore`: a restore can report success and change nothing.

## What wamover adds to WaBridge
Every rule was learned from rows WhatsApp itself wrote on a real iPhone, never guessed.

- **Message order:** `ZSORT` is renumbered to −N…−1, so new messages land below the history.
- **Chat list:** sorted by each chat's newest real message, not by WhatsApp's own notices (which carry the restore date).
- **Media:** photos, videos, voice notes, documents, GIFs and stickers are written in WhatsApp's own format:
  - path `Media/<chat>/<x>/<y>/<uuid>.<ext>`
  - SHA-256 and MIME type
  - pixel size
  - a 96 px JPEG thumbnail
  - folder mode 755
  - the message flag value for a downloaded file of that type, direction (sent/received) and chat kind (1:1/group)
  - documents: the `doc-YYYY-MM` gallery label, file name, page count and PDF thumbnail
  - stickers: their real size and no gallery label
- **No guessing:** a media kind is only migrated if the iPhone already holds a downloaded example of it.
- **Links** get entries in the Links tab.
- **Groups:** Android participant lists are not imported, and message senders are linked to WhatsApp's own member rows. See [Groups](docs/path-b.md#groups).
- **Archive status** and **LID chats** carry over (a person's LID chat and phone-number chat become one chat).
- **Stages:** you can migrate in stages (`--since`, `--chats`), and a later stage skips messages already on the iPhone.
- **Missing attachments:** `find-files` searches the Mac and the phone for attachments that were never downloaded on the phone, matched by WhatsApp's own SHA-256.
- **Safety:**
  - the Android phone and Finder's backup are only read, with checksums before and after
  - copies are APFS clones
  - private files are owner-only
  - nothing is written to Finder's folder without `--apply`
  - wamover never contacts WhatsApp's servers

## Commands
```
uv run wamover status                       # what's done, and the next command
uv run wamover doctor                       # adb, Finder backups, disk space, file permissions
uv run wamover pull                         # Android -> work/android (read-only on the phone)
uv run wamover decrypt work/android/Databases/msgstore.db.crypt15 --key work/backup.key
uv run wamover inspect work/msgstore.db
uv run wamover export work/msgstore.db --media work/android/Media       # HTML/JSON archive
uv run wamover plan work/msgstore.db --since 2026-01-01                 # CSV to choose chats
uv run wamover find-files work/msgstore.db --phone                      # optional
uv run wamover ios-prepare work/msgstore.db "<Finder backup folder>" --work-dir work/stage1 \
    --media work/android/Media --skip-undownloaded --archived \
    [--since 2026-01-01] [--recovered work/recovered/collected.json]
uv run wamover verify --work-dir work/stage1
uv run wamover install-backup --work-dir work/stage1 [--apply]           # dry run unless --apply
uv run wamover check-restore "<new Finder backup folder>" --work-dir work/stage1
```
Step by step, including which taps to make on the phones: [docs/path-b.md](docs/path-b.md). The official route first: [docs/path-a-checklist.md](docs/path-a-checklist.md).

## Known risks
- **Unofficial.** WhatsApp does not support this. It uses the official app on both phones and never signs in through anything else, but WhatsApp can change its database at any time.
- **Account restriction.** After four Finder restores in one day (each one makes WhatsApp set itself up again in about 900 groups), the account got a 24-hour restriction. **Do one restore, or at most one a day.** Never request verification codes repeatedly.
- **Old notices stay.** If you used a version before the group fix, WhatsApp may have posted "… left" notices. `--cleanup-groups` stops the cause, but it does not remove notices that are already there.
- **The restore replaces the whole iPhone** with the backup. Unencrypted backups leave out saved passwords, Health and Home data, so turn on iCloud sync for those first.
- Attachments that were never downloaded on the Android phone cannot be migrated.

## Licence
GPL-3.0-or-later, because WaBridge is GPL-3.0-or-later. See [LICENSE](LICENSE). WhatsApp is a trademark of Meta Platforms, Inc. This project is not affiliated with Meta or Apple.
