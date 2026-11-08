# Saint's review of DESIGN-WITNESS.md (WhatsApp, 8 Nov 17:23 WAT), condensed from the chat

**Verdict:** the thesis is the best in this hackathon; this plan is not, yet. Tier 1 is six features, three new wire contracts, a new job type and a second consent surface, in under 24 hours, on top of an open revoke bug. (The owner has since overruled cutting anything. Keep every feature, and use these points as design constraints.)

## What a judge attacks

- **"This is dirtside with a console window."** The transcript is only output. The parts a prompt cannot do are the hook, the device-side stop and the fixed checkup, and the video must say so in one sentence.
- **"The consent screen is theatre."** She presses Y because her son is on the phone telling her to. Microsoft's guidance cuts against us, because the product exists to be started by someone else.

## Feasibility, as Saint checked it in the code

- **F2 is real and the strongest claim.** The approve gate is at hermes_cli/plugins.py:1987-2016 and fails closed on error or timeout. The hook receives session_id (plugins.py:1924); the gateway binds the session key around the turn (gateway/run_turn_runner.py:980-1051); Hands already reads that same key with tools.approval.get_current_session_key (hands/environment.py:456).

  Three caveats:
  - the hook fires in every session, so cache the lookup;
  - the target can be server, so check the target first;
  - core already approves dangerous commands, so without a read-only allowlist it will prompt twice.
- **F3 is small.** The relay branches on type at hands/relay.py:619-628 (exec, screenshot), so checkup is one branch plus device dispatch.
- **F5 is risky.** Nothing reads the console today, and serve is one loop with a heartbeat thread (device loop.py:317-327). A 30 second prompt must run off the loop, or the device goes offline mid-prompt.
- **F7 has no path today.** ctx.inject_message is real (plugins.py:588), but it needs plugins.entries.<id>.allow_gateway_injection: true (plugins_injection.py:36-39) and runs only inside the gateway process. The relay lives in the dashboard process, so there is no route from her machine to the owner's phone without a new polling loop.

## Saint's suggested shape (now constraints, not cuts)

- Fix the revoke child leak first, because Stop reuses that path.
- Do the F6 internet run early (VPS plus phone hotspot), not last.
- **F1:** a consent screen, eight fixed labels with a "Running a command" fallback, S stop, and a receipt file with a printed path and a signature.
- **F5:** one consent at session start ("may your helper see your screen this session"). Saint suggested replacing per-request prompts with this; the owner keeps all features.
- **F2:** as designed, witness targets only, with an allowlist of read-only diagnostics.
- **F3:** six checks (disk, memory, Wi-Fi and DNS, internet, print queue, startup count).
- **F4:** one playbook demoed (disk full), three written.

## README safety section draft (Saint's text, verbatim)

**How this is different from a tech support scam.** A tech support scam works because the person on the phone starts the session, hides what they are doing, and cannot be stopped. This is built the other way round, and each line below is enforced by code on the computer, not by good manners.
- **She starts it.** Witness mode only runs when the person at the keyboard types Y to a screen that names who paired the machine. The helper cannot start it, and a session that has been stopped cannot be restarted from his side.
- **Every command is printed as it runs**, on her screen, with the time and a plain label. If Hands has no label for a command it prints the command itself rather than a friendly description.
- **The screen is shared only if she says yes**, once per session, and the answer travels back to the helper as an answer either way.
- **One key stops everything.** S ends the session, kills running work, and from that second the machine refuses new work, including work already queued.
- **Nothing listens on her machine.** The laptop dials out over HTTPS, both ends sign with Ed25519 keys pinned at pairing, and an offline or revoked machine fails closed.
- **She keeps the record.** A plain text receipt is written to her own disk: who helped, what ran, what changed, when it ended.
- **The helper still has to approve every change**, before it runs, on his own phone.

**What this does not fix:** if the helper's own account is stolen, the thief can drive a machine that is already paired. The consent screen, the transcript and the stop key are what limit the damage, and pairing a new machine always needs a code typed on that machine.

## 90 second demo (Saint)

Split screen: the phone in Telegram on the left, her laptop console on the right.

| Time | What happens |
|---|---|
| 0:00 | "My mum's laptop is slow. She is in another city. Watch what she sees." |
| 0:08 | She opens the console and reads: "Danny's Hermes wants to help. It can run commands and change files. It cannot control your mouse. Stop any time with S." She types Y. |
| 0:15 | Phone: "Run a checkup." Her screen: "Checking your disk, memory, wifi and printer." Five seconds later the phone shows "Disk C is 96 percent full. 18 GB is in Downloads." |
| 0:30 | Phone: "Clear temporary files." Telegram asks the helper to approve, and he taps Allow. Her screen shows "Clearing temporary files", then the exact command, then "done". A recheck shows 96 percent becomes 71. |
| 0:45 | Phone: "Let me see your screen." Her console asks and she presses N. Phone: "She said no." |
| 0:55 | Phone: "Now the printer." Her console still shows the last command. She presses S. Screen: "Stopped. Nothing else will run." Phone, within a second: "The person at the laptop stopped the session." |
| 1:10 | Her receipt opens: three steps, one change, stopped by her. |
| 1:20 | Close: "Help your mum's PC from your phone. She sees every step, and she holds the off switch." |

Her machine must be a different machine; her face is optional. Do no editing of the stop beat. If the stop beat is not live, cut the video, not the feature.
