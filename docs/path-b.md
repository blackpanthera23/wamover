# Path B: wamover

Run `uv run wamover status` at any point. It prints what's done and the next command.

## Folders
- `work/` holds the Android side: `android/` (crypt15, Media, contacts, `pull.json` with checksums), `backup.key`, `msgstore.db` (decrypted, private).
- `work/stage1/`, `work/stage2/`, ... hold one iPhone attempt each. A stage folder is never reused; every `ios-prepare` needs a new one.
- `archive/` is the readable HTML/JSON copy of the full history, with media.
- Git ignores all of these.

## Safety net (before touching the iPhone)
1. `work/android/Databases/msgstore.db.crypt15` matches the phone byte for byte (`pull.json` records both SHA-256 values).
2. The 64-digit key is in `work/backup.key`. Also keep it in a password manager: without it, neither the local nor the Google Drive backup can be opened.
3. `archive/html/index.html` holds every chat, with photos, videos and voice notes.
4. The Android phone keeps its WhatsApp data. Do not uninstall WhatsApp there or clear its storage until the iPhone has been checked.
5. Optional: copy `work/android/Databases/msgstore.db.crypt15` (3 GB) and the key to an external drive.

## Security
- **Key:** `work/backup.key` is mode 600 (only you). Keep it also in a password manager. Anyone with it plus the backup file can read all chats.
- **No network to WhatsApp:** wamover never contacts WhatsApp's servers. The only network in the stack is the desktop app we don't use.
- **No key on the command line:** crypt15 decryption runs in-process, so the key never appears in `ps` or a crash log.
- **Permissions:** `work/`, `archive/` and their contents are owner-only (700/600). `doctor` checks the key and database stay that way.
- **Git:** `work/`, `archive/`, `*.key`, `*.crypt*`, `msgstore.db*`, `.tools/`, `work-*/`, `archive-*/` are git-ignored, so a stray `git add -A` won't commit chats or the key.
- **Android phone:** read-only over adb. Nothing is written, no root, no WhatsApp uninstall.

## Choose what goes over
Migrate a subset first, check it on the iPhone, then add the rest in a later stage. Stages add up: messages already on the iPhone are skipped by WhatsApp message ID, so nothing is duplicated.

```
uv run wamover plan work/msgstore.db --since 2026-01-01 --out work/chats-2026.csv
```
This lists every chat, biggest first, with message and media counts for the period. Chats with messages in 2026 are pre-marked `y`. You don't have to edit it. To leave chats out, open it in Numbers, clear `y` on those rows, and export as CSV.

## iPhone stage
iPhone: install WhatsApp, register the **same number**, send one message. Finder > iPhone > untick **Encrypt local backup** > **Back Up Now**. Give Kiro Full Disk Access so it can read the backup folder.

```
uv run wamover ios-prepare work/msgstore.db "$HOME/Library/Application Support/MobileSync/Backup/<UDID>" \
    --since 2026-01-01 --work-dir work/stage1          # add --chats work/chats-2026.csv to use your edits
uv run wamover verify --work-dir work/stage1
uv run wamover install-backup --work-dir work/stage1           # dry run
uv run wamover install-backup --work-dir work/stage1 --apply   # renames Finder's old backup, never deletes
```
`ios-prepare` only reads Finder's backup (checksum before and after) and clones it into the stage folder, which costs almost no disk space on APFS. Without `--media`, photos and videos show as empty bubbles; the archive keeps them viewable.

Then: Find My iPhone **off** > Finder > **Restore Backup** > newest > wait for the lock screen > Find My back on > open WhatsApp.

## Media and links (`--media work/android/Media`)
wamover copies how WhatsApp on *your* iPhone stores media, from rows WhatsApp itself wrote in the backup:
file at `Media/<chat>/<x>/<y>/<uuid>.<ext>` (backup path `Message/` + that), SHA-256 and MIME type,
size and pixel dimensions, a JPEG thumbnail (longest side 96 px) next to it as `.thumb`, folder mode 755,
and the message flag value WhatsApp uses for a downloaded file of that type.

A combination of type (photo, video, voice, document, GIF, sticker), direction (sent/received) and chat
kind (1:1/group) is only migrated when the iPhone already holds a downloaded example of exactly that
combination. Everything else stays as an empty bubble and is listed in `stage.json`. To cover more,
send and receive one of each type in a 1:1 chat and in a group on the iPhone (with media auto-download on)
before taking the Finder backup.

Links in text get one entry each in WhatsApp's Links list, in the same shape as WhatsApp's own rows.
Media files are APFS-cloned into the backup, so they take no extra disk space there.

## Groups
WhatsApp already knows which groups you are in and who is in them: it downloads that when you register on the iPhone. wamover therefore never writes group membership.
- Android's participant lists are **not** imported. Android also lists you as a placeholder (`lid_me`) and everyone else by phone number, while WhatsApp on iOS lists people by LID.
- A group message is linked to WhatsApp's own member row for its sender: the phone number is mapped to the LID through Android's `jid_map`. Only if there is no such row is a new one made, and it is **inactive**, the way WhatsApp keeps people who have left.
- After the import, any new member row that is active, or that is `lid_me`, stops the run or is set inactive.

An earlier version did copy the participant lists. After the restore, WhatsApp then posted "… left" notices in hundreds of groups and pushed those chats to the top of the chat list. If your iPhone already went through that version, `--cleanup-groups` sets those member rows inactive. It cannot remove notices WhatsApp has already posted.

## Restores and your account
Each Finder restore makes WhatsApp set itself up again on that iPhone. In a real migration with about 900 groups, four restores in one day led to a **24-hour account restriction**. Plan for **one** restore: migrate everything in a single stage once you have tested on a small subset, and leave at least a day between restores. Enter verification codes once, and never ask for a new code repeatedly. If you are restricted, wait it out and don't restore, reinstall or re-register in the meantime.

## Check it
Finder > **Back Up Now**, then:
```
uv run wamover check-restore "$HOME/Library/Application Support/MobileSync/Backup/<UDID>" --work-dir work/stage1
```
Also look at a few chats on the iPhone: order, names, a group, a message from today.

## Next stage
Once stage 1 is good, use the backup you just made as the source:
```
uv run wamover ios-prepare work/msgstore.db "$HOME/Library/Application Support/MobileSync/Backup/<UDID>" --work-dir work/stage2
```
No `--since` means everything; the 2026 messages are already there and get skipped. Repeat verify, install, restore, check.

## Timing
Messages that arrive on the iPhone between its Finder backup and the restore are lost from the iPhone, because the restore puts the iPhone back to that backup. Do backup, `ios-prepare`, install and restore in one sitting. Reading 3 million Android messages takes about 8 minutes of that.

## Rollback
- Finder's original backup stays in Finder's list under a renamed folder.
- Each stage folder has an untouched `ios_backup_pristine/` copy.
- The Android phone and `work/android/` are never modified.
