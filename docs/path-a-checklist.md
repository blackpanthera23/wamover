# Path A: official Move to iOS retry (WhatsApp Android to iPhone)

Path B code is ready in this repo, but do not restore any modified backup onto the iPhone while this retry is running. See `docs/path-b.md` only after this attempt has failed.

Requirements: same phone number on both phones, latest WhatsApp on both, SIM in the Android phone, iPhone new or factory reset.

## Before you start
- [ ] Restart (power off and on) the Android phone. No factory reset needed.
- [ ] Update WhatsApp on the Android phone.
- [ ] Charge both phones, plug the Android in.
- [ ] Factory reset the iPhone (Settings > General > Transfer or Reset iPhone > Erase All Content and Settings).
- [ ] Do NOT touch the custom tool or modify the iPhone backup while retrying this.

## Transfer
- [ ] On the iPhone setup screen choose "Move Data from Android" and enter the code in the Move to iOS app.
- [ ] Use Wi-Fi only. Do not use a cable.
- [ ] Select ONLY WhatsApp in the Move to iOS app (untick everything else).
- [ ] Keep both screens awake. Tap each screen every 20 to 30 seconds.
- [ ] Expect up to about 45 minutes. A stall at 93% has been reported by others.
- [ ] Finish iPhone setup, install and open WhatsApp, log in with the same number, and tap Start when asked to import.

## If it worked: bring the rest of your data over
- [ ] WhatsApp on iPhone > Settings > Chats > Chat Backup > Back Up Now (iCloud).
- [ ] Factory reset the iPhone again and set it up with Move to iOS, this time selecting everything EXCEPT WhatsApp.
- [ ] Install WhatsApp and restore from the iCloud backup.

## If it failed
- [ ] Restart both phones and repeat once.
- [ ] Keep the original Android WhatsApp data untouched (do not uninstall WhatsApp).
- [ ] Tell me, and we move to the custom tool (Path B).
