# Hermes Hands v0.2 design: help any computer from chat, with the person at the machine in control

Status: a draft for review by Codex, the Hermes worker and Saint. Nothing is built yet.

Owner goal: the number 1 plugin in the Nous Research Hermes Agent Plugin Hackathon.

| Deadline | Time (WAT) |
|---|---|
| Code freeze | Mon 9 Nov 14:00 |
| Catalog PR | Mon 22:00 |
| Pin freeze | Tue 14:00 |
| Final deadline (X video + form) | Wed 11 Nov 23:00 |

Judging, from the official announcement: usefulness, reliability, polish, creativity, and doing more than a prompt or skill could.

## 1. Thesis

**Today.** Every entry in this space, dirtside included (PR 149883: an open hackathon entry with 654 tests and a live internet run), answers one question: "how does my server Hermes reach MY machine?" The only human they model is the owner.

**The problem we take on is different.** The machine that most needs help belongs to someone else: a parent, a grandparent, a friend. That person is not technical, is frightened of scams, and is the one sitting at the computer. Today the options are a long phone call, or talking them through installing TeamViewer.

**Hermes Hands v0.2 makes helping someone else's computer safe, from chat.**

| Who | What they do |
|---|---|
| The helper | Chats with Hermes from their phone |
| Hermes | Diagnoses and proposes fixes |
| The person at the computer | Starts the session, sees every real step in plain words, approves screen access per request, and can stop everything with one key |
| The helper (again) | Approves every change on that machine |
| Everyone | Gets a receipt |

One line: **"Help your mum's PC from your phone; she sees every step and holds the off switch."**

**Why it is a plugin and not a prompt.** Every guarantee below is enforced by code on the device or by a Hermes hook, not by the model:
- the device prints the real command;
- the device refuses work after Stop;
- the device asks before a screenshot;
- a pre_tool_call hook forces owner approval;
- the checkup runs fixed, signed commands.

**How it improves Hermes.** Hermes's approval model assumes one human. Hands adds a second human principal, "the person at the machine", with less authority than the owner and a local veto. It is a reusable pattern, and we will propose it upstream in the hook issue.

## 2. What already works (live-tested, 8 Nov)

Pairing, plus terminal and file tools on a native Windows laptop with no Hermes on it. Specifically:
- a Telegram message through Hermes (Claude Sonnet 5.5 via the Nous Portal) to a real command on the laptop;
- approval prompts in Telegram before dangerous commands;
- fail-closed behaviour when the relay is down;
- the switch to server;
- background jobs across a switch;
- byte-exact files, 1 MB output and timeouts;
- validators green;
- browser placement.

**Open:** gate 2 check 5. After a revoke, a job child stayed alive for 20+ s. The fix is in progress.

## 3. Features, by tier

### Tier 1: must ship by the Mon 14:00 freeze

**F1. Witness Mode (device).** `hermes-hands serve --witness`.
- Foreground only. It refuses without a real console, and refuses under install-service's pythonw.
- **Consent screen.** It states:
  - who paired this machine, by owner and gateway host;
  - what the helper can do: run commands and see and change your files, with no mouse or keyboard control;
  - that the screen is shared only if you say yes each time;
  - "press S at any time to stop".

  She types Y to begin. She starts the session herself; Microsoft's guidance is to accept help only when you started it.
- **Live transcript.** Each job prints:
  - a timestamp;
  - a plain label from a FIXED device-side table (for example "Checking your Wi-Fi"), so the model cannot write a misleading label;
  - the verbatim command (trimmed);
  - the outcome ("done", "failed", "stopped").

  An unknown command prints "Running a command" with the verbatim text.
- **One-key Stop (S).**
  - Cancel and terminate every running job tree.
  - Post a done result for each one with the error "the person at <device> stopped the session".
  - Refuse any job fetched after that.
  - Flush the results within a bound of about 5 s, then exit `serve`.

  The helper's phone then shows the stop reason for in-flight work, and "offline, nothing ran" for anything new.
- **Receipt.** On stop or exit, write a plain text file to her Documents folder (or `~/.hermes-hands/sessions/` as a fallback): who helped, each step (label, verbatim command, outcome, time) and what changed. Print its path.
- **Heartbeat flag.** Heartbeat and poll bodies carry `"witness": true` while this mode runs.

**F2. Owner approves every change on a witness machine (gateway).**
- A `pre_tool_call` hook. This seam is verified: `hermes_cli/plugins.py`, `_resolve_block_from_details`, where action "approve" calls `tools.approval.request_tool_approval` and fails closed.
- When the conversation's target is a device whose last heartbeat says witness:
  - require approval for every terminal and file write or patch call;
  - except an exact allowlist of the read-only checkup and diagnostic commands.
- Non-witness devices keep today's behaviour, so developers do not get approval fatigue.
- The relay stores the witness flag per device.

**F3. One-tap Checkup: a report card for the machine (both halves).**
- A new tool `hands_checkup(device)` and a new job type `checkup`, mirroring the existing `screenshot` job path.
- The relay accepts it. The device refuses unknown types explicitly; today they hang.
- The device runs a FIXED, read-only set of Windows checks (no model-authored commands): disk free, memory and top processes, Wi-Fi and DNS state, internet reachability, the print queue, pending reboot or update hints, battery, and the startup program count.
- It returns structured JSON: field, value, and a status of ok, warn or bad.
- Hermes turns that into a plain-language report: "Disk C is 96% full; that is why it is slow."
- **Demo value:** a whole PC health report in about 5 seconds, from a phone, which cannot hallucinate because the device ran fixed checks.

**F4. Help Desk playbooks (skill).** For 5 common problems (no internet, printer stuck, slow PC, disk full, program frozen):
- diagnose read-only first;
- propose a fix;
- say when a fix needs the person to click a Windows permission (UAC) prompt;
- after any fix, re-run the check and state "fixed" or "not fixed" with before and after values.

Every fix command is tested on the real laptop for admin needs before it goes in the skill.

**F5. Consent per screenshot (device).** In witness mode, `hands_screenshot` prompts on the device console ("Your helper wants to see your screen. Allow? Y/N", 30 s timeout means No). The answer goes back as an error or an image.

**F6. Maturity parity with dirtside.**
- A live run over the real internet: gateway on a VPS, the laptop on a different network such as a phone hotspot.
- A Linux device test, with the device agent in WSL Ubuntu.
- CI green on GitHub, which needs the owner to fix billing or make the repo public.
- Honest disclosure for macOS.

### Tier 2: only if Tier 1 is green by Mon 10:00

**F7. She can ask for help (device to owner).** In witness mode she presses H and types "my printer won't print". The device sends a signed help request, and the owner's Telegram gets "Mum's PC asks for help: my printer won't print", with a reply to start.

This needs a gateway path to message the owner. The plugin seam for sending a platform message is UNVERIFIED and must be checked first. If no clean seam exists, show the request in `/hands status` and the Desktop pane instead.

**F8. Session timer.** Witness sessions auto-end after N minutes, shown on her screen.

### Tier 3: roadmap only (state it in the PR, do not build)

Multilingual transcripts; a tray icon instead of a console; macOS and Linux witness consoles; a family dashboard of several machines' checkups over time.

## 4. Safety story (README section: "How this is different from a tech support scam")

| Scam | Hermes Hands |
|---|---|
| The caller initiates | She starts the session at her keyboard and types Y. Pairing needs a one-time code typed on her machine. |
| Hidden control | Every real command is printed on her screen as it runs. |
| Screen grabbed silently | Screen only after she says yes, each time. |
| Cannot be stopped | One key stops everything, and the device refuses further work. |
| Inbound remote access | Outbound only, with nothing listening. Ed25519 signed both ways. |
| No record | A receipt is saved on her own disk. |
| Free rein | The helper must approve every change, and the device keeps its own deny list. |

## 5. Demo, 75 to 90 seconds, with a real second person and two devices

Split screen: the helper's phone (Telegram) on one side, her Windows laptop console on the other.

| Time | What happens |
|---|---|
| 0 to 10 s | She starts witness mode, reads the consent screen and types Y. |
| 10 to 20 s | The helper types "Mum says her laptop is slow, run a checkup". Her screen shows "Running your PC checkup". The phone shows the report: "Disk C is 96% full; 18 GB in Downloads". |
| 20 to 40 s | The helper asks it to clear her temp files. Telegram shows the approval prompt, and he taps Allow. Her screen shows "Clearing temporary files" with the verbatim command, then "done". A re-check shows the before and after values. |
| 40 to 55 s | He asks to see her screen. Her console asks Y/N and she presses N. The phone shows "she declined". |
| 55 to 70 s | During the next step she presses S. Her screen shows "Stopped. Nothing else will run." Within a second the phone says the person at the laptop stopped the session. |
| 70 to 85 s | The receipt opens on her desktop. Close on the line: "Help your mum's PC from your phone; she sees every step and holds the off switch." |

## 6. Lanes

| Owner | Work |
|---|---|
| **Codex** (max effort, device) | Finish the revoke fix first. Then F1, F3 device side, F5, the heartbeat flag, the label table, and tests. |
| **Hermes worker** (gateway) | F2 hook and witness flag storage, F3 tool and relay job type, F4 skill, messages, and README and PR text. |
| **Saint** | Brutal critique of this design, the safety copy, the demo script, and a watch for prior art as entries land. |
| **Orchestrator** | Contracts, integration, live tests on the real laptop and VPS, and git. |
| **Opus and Fable** | Advisors only. |

## 7. Wire contract changes (both halves must match)

- Heartbeat and poll bodies get optional `"witness": bool`.
- A new job type `checkup`, `{job_id, type: "checkup"}`, whose result carries a `checkup` JSON object alongside the existing fields.
- A device result `error` value, "stopped_by_person", that the gateway shows as a plain message.
- An explicit device refusal result for unknown job types.

## 8. Questions for the reviewers

1. Is this the number 1 design? What is missing, what is weak, and what would a Nous judge or dirtside's author attack?
2. Is the Tier 1 scope buildable and reliable by Mon 14:00 with Codex plus the Hermes worker? What should be cut or reordered?
3. Feasibility of each feature against the real code, citing file:line:
   - F2's hook and how it finds the target and the witness flag;
   - F3's new job type;
   - F5's console prompt while serve runs;
   - F7's owner notification seam.
4. Any better feature that beats one in Tier 1 for the same hours.
