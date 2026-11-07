---
name: hands
description: "Work on a paired Hermes Hands device: Git Bash paths on Windows, what hands errors (255, 126) mean, and device screenshots."
---

# Hermes Hands

When the user points this conversation at a device with `/hands use <device>`, your `terminal`,
`read_file`, `write_file`, `patch` and `search_files` calls run on that device as its user, not on
the Hermes server. `/hands use server` points them back at the server. Only the user switches the
target, and a switch applies from the next command.

## Know where you are

The first `terminal` result after a switch, or in a new session, starts with a target note:

- `hands: now on laptop (Windows-11-..., Git Bash, home /c/Users/ridwan; use /c/... paths)`
- `hands: now on laptop (Linux-..., home /home/ridwan)`
- `hands: now on server (the gateway host)`

Take paths from the note's home. Do not reuse paths from the server (or from another device) after
a switch; they do not exist there. `hands_devices` lists the paired devices and whether each is
online.

## Windows devices (Git Bash)

- Commands run in Git Bash: bash, coreutils, `find` and `grep`. Write bash, not PowerShell or cmd.
- Use Git Bash path forms everywhere, in commands and in file tool arguments: `C:\Users\ridwan\notes.txt`
  is `/c/Users/ridwan/notes.txt`, `D:\work` is `/d/work`. Forward slashes, lowercase drive letter.
- Quote paths with spaces: `"/c/Users/ridwan/My Projects/app"`.
- Prefer absolute paths built from the note's home over `~`.
- Root `search_files` at the narrowest folder you can (`/c/Users/ridwan/projects`), not the home
  folder or a drive root: there it walks every file under that root, AppData included, which is slow.

## Reading hands errors

**Exit 255 and a line starting `hands:`** means nothing ran anywhere; there is no fallback to another
machine. The line names the cause:

- `... is offline, so nothing ran` (the device agent is not running or the laptop is asleep)
- `no paired device is named ...` (revoked or never paired)
- `the relay ... is not reachable, so nothing ran on the device` (the dashboard on the gateway is down)
- `the deny-list on ... changed since it was paired` (the relay refuses that device)

Tell the user the cause in one sentence and suggest `/hands use server` to keep working on the
server, or the fix the line names (start `hermes-hands serve` on the device, or `/hands accept-policy
<device>` if they changed the deny-list themselves). Do not retry in a loop, and do not try to reach
the device some other way.

**Exit 126 and `Policy refused command matching: <pattern>`** means the device's own deny-list refused
the command. Respect it: do not reword, encode, split, alias or script around it, and do not try
another shell. Tell the user which pattern refused it; only they can change that policy, on the
device itself.

Also: 124 is a timeout, 137 a cancelled command, and `went offline before the command finished; any
output above is partial` means the output you have is incomplete.

## Screenshots

Use `hands_screenshot` when the user asks what is on the device's screen, or to check a result you
cannot confirm from the shell (a window, a dialog, a page rendering). It captures the device this
conversation targets; it refuses on the `server` target and in cron.

- On success the result contains a line `MEDIA:/absolute/path.png`. Copy that exact line into your
  reply (that is how the image reaches the user's chat), then call `vision_analyze` on that path to
  describe what is on screen.
- `Screenshot capture requires serve --allow-screen` means the user has not consented to screen
  capture on that device. Tell them; do not retry.
