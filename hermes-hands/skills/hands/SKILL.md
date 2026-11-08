---
name: hands
description: "Work on a paired Hermes Hands device: Git Bash paths on Windows, what hands errors (255, 126) mean, and device screenshots."
---

# Hermes Hands

When the user points this conversation at a device with `/hands use <device>`, your `terminal`,
`read_file`, `write_file`, `patch` and `search_files` calls run on that device as its user, not on
the Hermes server. `/hands use server` points them back at the server. Only the user switches the
target, from a direct chat with the bot, and a switch applies from the next command. The target belongs to the chat, so `/new` and
`/reset` keep it: a fresh conversation in a chat that was on a device still runs there.

## Know where you are

The first `terminal` result after a switch, or in a new session, starts with a target note:

- `hands: now on laptop (Windows-11-..., Git Bash, home /c/Users/ridwan; use /c/... paths)`
- `hands: now on laptop (Linux-..., home /home/ridwan)`
- `hands: now on server (the gateway host)`

Take paths from the note's home. Do not reuse paths from the server (or from another device) after
a switch; they do not exist there. `hands_devices` lists the paired devices and whether each is
online.

Terminal commands on a device start in its home folder. A relative path in `read_file`,
`write_file`, `patch` or `search_files` resolves against the device's current terminal folder, but
a working folder configured on the gateway still wins over it, so on a device prefer absolute device
paths built from the note's home (`/c/Users/ridwan/...` on Windows, `/home/ridwan/...` on Linux). A
`~/...` path lands in the device home, though the tool result may show the server's home in its
`resolved_path`.

Do not use `sudo` on a device: its default deny list refuses it with exit 126.

## Windows devices (Git Bash)

- Commands run in Git Bash: bash, coreutils, `find` and `grep`. Write bash, not PowerShell or cmd.
- Use Git Bash path forms everywhere, in commands and in file tool arguments: `C:\Users\ridwan\notes.txt`
  is `/c/Users/ridwan/notes.txt`, `D:\work` is `/d/work`. Forward slashes, lowercase drive letter.
- Quote paths with spaces: `"/c/Users/ridwan/My Projects/app"`.
- Prefer absolute paths built from the note's home over `~`.
- Git Bash turns a `/switch` argument of a native Windows program (`cmd`, `tasklist`, `ipconfig`,
  `taskkill`, `schtasks`) into a path, so `cmd /c ...` runs nothing and still exits 0. Run such a
  program with `MSYS_NO_PATHCONV=1` in front and its switches written normally, with one slash:
  `MSYS_NO_PATHCONV=1 tasklist /FO CSV`, `MSYS_NO_PATHCONV=1 ipconfig /all`. Never double the slash
  (`//c`, `//F`): that spelling hides the command from Hermes's approval check, which only knows the
  single slash form. Under `MSYS_NO_PATHCONV=1`, write that program's path arguments in Windows form,
  quoted, such as `'C:\Users\ridwan\notes.txt'`, because `/c/...` paths stop working there. Leave
  path conversion on for everything else.
- Root `search_files` at the narrowest folder you can (`/c/Users/ridwan/projects`), not the home
  folder or a drive root: there it walks every file under that root, AppData included, which is slow.

## Reading hands errors

Hands reports its own failures with **exit 255 and a line starting `hands:`**. The exit code alone
does not say what happened, since a command can exit 255 by itself; the `hands:` line does, and it
says whether the command may have run.

When the line says **nothing ran**, nothing ran anywhere; there is no fallback to another machine.
The line names the cause:

- `... is offline, so nothing ran` (the device agent is not running or the laptop is asleep)
- `no paired device is named ...` (revoked or never paired)
- `the relay ... is not reachable, so nothing ran on the device` (the dashboard on the gateway is
  down). In the rare case that it went down just as it took the command, the command may still run,
  so check before you repeat anything that is not safe to run twice.
- `the deny-list on ... changed since it was paired` (the relay refuses that device)
- `the command or its input is longer than 16 million characters, so nothing ran` (split the work,
  or write the file in smaller parts)
- `the relay cannot read the Hermes Hands store ... and refused this request` (the relay refused the
  command before it ran; the cause is in the dashboard log on the gateway, so tell the user)

Tell the user the cause in one sentence and suggest `/hands use server` to keep working on the
server, or the fix the line names (start `hermes-hands serve` on the device, or `/hands accept-policy
<device>` if they changed the deny-list themselves). Do not retry in a loop, and do not try to reach
the device some other way.

When the line says the command **may have run**, it started on the device and Hands lost track of
it, so it may have partly run or may still be running there, and any output you have is partial:

- `contact with the relay was lost after the command started on ..., so it may still be running there`
- `the device agent on ... restarted while the command ran, so it may have partly run there`
- `... went offline before the command finished; any output above is partial`
- `the deny-list on ... changed since it was paired, so the relay stopped the command, which may have
  partly run there` (tell the user; only they can accept the new policy with `/hands accept-policy`)

Do not run it again until you have checked its effect on the device (with `ls`, `git status` or
`ps`, say), and tell the user what you found.

**Exit 126 and `Policy refused command matching: <pattern>`** means the device's own deny-list refused
the command. Respect it: do not reword, encode, split, alias or script around it, and do not try
another shell. Tell the user which pattern refused it; only they can change that policy, on the
device itself.

Also: 124 is a timeout and 137 a cancelled command. Output that reaches the device's 64 MiB cap stops
the command there; the result then has a non-zero exit code and a line naming the cap, and the output
is cut short.

## Screenshots

Use `hands_screenshot` when the user asks what is on the device's screen, or to check a result you
cannot confirm from the shell (a window, a dialog, a page rendering). It captures the device this
conversation targets; it refuses on the `server` target and in cron.

- On success the result contains a line `MEDIA:/absolute/path.png`. Copy that exact line into your
  reply (that is how the image reaches the user's chat), then call `vision_analyze` on that path to
  describe what is on screen.
- `Screenshot capture requires serve --allow-screen` means the user has not consented to screen
  capture on that device. Tell them; do not retry.
