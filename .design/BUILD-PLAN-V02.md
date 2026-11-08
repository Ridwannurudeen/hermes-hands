# Hermes Hands v0.2 Build Plan

Status: full-scope implementation plan. Features F1 through F8 are all included. This document plans the build only. It does not change the repository.

## 1. Objective and constraints

Hermes Hands v0.2 adds a consent-led witness session for remote PC support. The owner explicitly kept every feature in scope, including Linux device parity and the H help-request flow. Preserve the current pairing, device signing, job relay, revocation, policy, and screenshot behavior unless a section below explicitly extends it.

The target is code complete by Monday, November 9, 11:00 WAT, when the Nous Portal limit resets. Run the live Windows, WSL Linux, VPS internet, and Telegram checks after that point. The PR is due Monday at 22:00 WAT. Freeze feature scope Monday at 14:00 WAT. The pin freezes Tuesday at 14:00 WAT.

The repo is at HEAD ce5c5b97de66b60129c3e4645cd464cd9c3f0f85. The device worktree has uncommitted 18B changes in device/hermeshands_device/executor.py, device/tests/test_executor.py, and device/tests/test_loop.py. The first implementation gate is to inspect that exact diff and prove the revoke fix still passes its focused tests. Stop must reuse the verified process termination path.

The Hermes source supplied at scratchpad/hermes-v0.23.0 was inspected for the hook, approval, gateway injection, and session-origin seams listed below. The supplied tree's package metadata reports version 0.0.0 and it has no Git metadata, so it does not prove that it is the exact v0.23.0 release. Before implementing a Hermes-specific seam, compare the source to the installed Hermes release or an exact v0.23.0 checkout. Do not silently code against a mismatched source snapshot.

## 2. Feature scope and implementation order

No feature is cut.

1. Reconcile and test 18B revoke handling first. Preserve the current PID and process creation-time verification and Windows descendant cleanup. The full repo test command is listed in the verification plan. Stop depends on this termination path.
2. Define and implement shared wire semantics independently in the device and gateway lanes. Keep the existing signed request protocol. Add typed fields without weakening signature verification.
3. Build F1 witness startup and console input, including session-start screenshot consent.
4. Build the Stop state machine and bounded result flush. Implement F5 screenshot consent through the same console input owner.
5. Build F8 session timeout and the signed local receipt.
6. Build F3's fixed checkup job and structured result path, then build the gateway consumer and result formatter.
7. Build F7's signed help request, owner-origin persistence, and gateway-side delivery poller.
8. Build F2's approval hook and exact read-only allowlist against the same owner-scoped target resolution used by the environment.
9. Add all five F4 playbooks, then run the full device, plugin, end-to-end, and live gates.
10. Update version and user-facing documentation only after the contract tests pass. Stop feature work at Monday 14:00 WAT; later changes should be limited to release-blocking fixes.

## 3. Wire contracts

All new device requests continue to use the existing canonical JSON and Ed25519 request signature over the method path and body. The relevant implementation is device/hermeshands_device/protocol.py:65-66 and 107-120. The relay verifies the device signature before handling a request in hermes-hands/hands/relay.py:336-358. The witness flag is therefore authenticated, not an unsigned dashboard preference.

### 3.1 Device poll request

POST /api/plugins/hermes-hands/device/poll. Preserve current fields and add:

- wait: integer, existing wait interval.
- policy_hash: string.
- instance: string.
- witness: boolean. True only after a person accepts the witness session consent.
- session_state: string enum: normal, active, stopping, stopped.
- capabilities: array of strings. v0.2 device values are witness-v1, checkup-v1, help-v1, receipt-v1.

A device with no active witness session sends witness=false and session_state=normal. During an accepted session it sends witness=true and session_state=active. During Stop or timeout it sends stopping, then sends stopped with witness=false after the result flush attempt. A request missing witness is interpreted as false and has no v0.2 capability. Reject malformed types. Do not infer true from a stored prior value.

### 3.2 Heartbeat request

POST /api/plugins/hermes-hands/device/heartbeat. Preserve the existing policy_hash, os, home, version, and instance fields. Add the same witness, session_state, and capabilities fields and type rules as poll. Existing field names are retained to avoid a needless protocol rename.

The relay stores witness, session_state, and the normalized capabilities on the device row in the same transaction that updates last-seen metadata. Additive SQLite migrations use the existing _add_column path in hermes-hands/hands/store.py:80-100. record_seen is the shared persistence seam at store.py:203-223. device_poll calls it at relay.py:490-537; device_heartbeat calls it at relay.py:590-613. Reads used for authorization must load the persisted device row, not trust client-supplied display data.

Older devices that omit witness remain non-witness devices. The gateway must not enqueue checkup or help jobs to devices whose stored capabilities omit the corresponding capability.

### 3.3 Checkup job and result

The gateway creates a job with exactly this semantic payload:

- job_id: string.
- type: string, exactly checkup.

There is no command, shell script, path, or caller-supplied diagnostic argument. The device executes only its compiled-in, fixed read-only checkup implementation. Unknown type handling is specified below.

A final device result uses the existing job result envelope and adds checkup:

- job_id: string.
- seq: integer.
- chunk: string.
- done: boolean.
- policy_hash: string.
- returncode: integer when done.
- error: string or null when done.- checkup: object, final result only.

checkup has this schema:

- schema_version: integer, initially 1.
- observed_at: RFC 3339 UTC timestamp string.
- platform: string, for example windows or linux.
- facts: array of objects with id string, label string, value string, number, boolean, or null, unit string or null, status enum ok/warn/bad/unknown, availability enum observed/unsupported/error, and source string.
- inferences: array of objects with claim string, evidence_ids array of fact id strings, and confidence enum low/medium/high.

The device reports observations in facts. The gateway may add clearly labeled possible inferences using only fact IDs. It must keep those in a separate inferences section and never present an inference as an observed fact. Unsupported and failed fields remain explicit. Do not turn this into arbitrary command execution.

The fixed checkup covers disk capacity, memory and top processes, Wi-Fi and DNS state, configured reachability probes, print queue state, pending reboot/update hints, battery where supported, and startup item count. Use per-probe time bounds and partial reports. A missing privilege or unavailable Linux API is an availability value, not a guessed result. Do not report personal process command-line arguments.

The result path is relay.py:538-588, job output at relay.py:688 onward, and client.py:134 onward. Extend each typed layer so checkup is not dropped by result storage or polling.

### 3.4 Unknown job refusal

The device explicitly refuses every unknown or malformed job type. It sends a final result with the existing envelope:

- job_id: string, if present and valid.
- seq: integer.
- chunk: empty string.
- done: true.
- policy_hash: string.
- returncode: 126.
- error: string exactly unknown_job_type.

It performs no action and creates a receipt event for the refusal. Add an explicit else branch in the device job dispatcher. Today the dispatcher handles exec, screenshot, and cancel but has no final unknown-type refusal: device/hermeshands_device/loop.py:208 onward, screenshot near 296, cancel near 299. The relay currently accepts only exec and screenshot in relay.py:624-686. Extend its enqueue path for checkup, not for arbitrary unknown types.

### 3.5 Stopped result

For each in-flight job interrupted by a person pressing S, send the normal final envelope with:

- done: true.
- returncode: 130.
- error: string exactly stopped_by_person.

Include any output already observed in earlier chunks. The gateway renders this as “Stopped by the person at the device.” Do not report a repair or state change unless a later observation confirms it.

For timeout, use error=session_timeout and returncode=124. The result distinguishes the two end reasons, and the receipt records the same reason.

### 3.6 Witness consent and session timeout

The witness device starts with command hermes-hands serve --witness. It must run in a real interactive console. Refuse witness mode if stdin or stdout is not a TTY, if running through pythonw, or if started as the background service. The existing serve option handling is in device/hermeshands_device/cli.py:142-163; the service launch uses pythonw in device/service.py:47-49. Do not silently fall back to unattended witness mode.

Before polling for witness work, show the paired owner label, gateway host, capabilities, session duration, and these limits: no mouse or keyboard control; commands and outcomes are recorded; screenshots require consent; S stops the session. Ask for Y/N consent. N exits without sending witness=true.

After the first Y, ask a separate session-start screenshot consent question: “May the helper ask for screenshots during this session? Y/N”. N means the device refuses all screenshot jobs for this session. Y permits the helper to ask, but does not grant a screenshot. Every individual screenshot still needs a second, per-request Y/N decision. That preserves the separate F5 control.

F8 uses a fixed 30-minute maximum session duration from the initial witness consent. It is an absolute session limit, not an inactivity timer and not extendable by activity. Use a monotonic deadline. Check the deadline before accepting each new job and immediately after process wake from sleep. On expiry transition to stopping, refuse new work, terminate active jobs, flush results within the same bound as Stop, and then exit.

### 3.7 Help request

The device's H handler asks for a short text message. It sends POST /api/plugins/hermes-hands/device/help with:

- request_id: string, unique lowercase 32-character hexadecimal identifier.
- instance: string, matching the signed in device instance.
- text: string, trimmed, 1 to 500 Unicode code points.

The request uses the normal device bearer token and signed body/path. The relay accepts it only for a paired, non-revoked device whose persisted witness flag is true, session_state is active, and help-v1 capability is stored. Apply a per-device rate limit, reject an empty or overlength message, and enforce a request body size limit before JSON parsing.

Persist each request with a unique request_id, device ID, timestamp, message, delivery state, and lease/retry metadata. A duplicate request_id returns the original accepted status and never creates a second row. Do not make the device wait for Telegram delivery.

## 4. Runtime design

### 4.1 One console input owner

Create one device console/input handler, running on its own thread or equivalent platform-specific reader, separate from poll and heartbeat. It owns all stdin reads for:

- Initial witness Y/N.
- Session-start screenshot Y/N.
- S stop.
- H help request and its text.
- Per-request screenshot Y/N.
Poll and heartbeat must keep running while a person is answering. Do not call blocking input from _run_job, _poll_body, _heartbeat_body, or the result delivery loop. Queue prompt requests to the console handler and return decisions through a thread-safe queue. Use prompt IDs so an expired screenshot prompt cannot accidentally answer a later prompt. The per-request screenshot deadline is 30 seconds; timeout, EOF, or any answer except explicit Y means No.

The handler keeps S reserved as the stop command. Outside the H text editor, either case of S stops. While composing a help message, uppercase S stops immediately and lowercase s is text. Escape cancels message composition. Document that rule in the console prompt and test it. H starts the message editor; an empty or canceled message is not submitted.

### 4.2 Stop state and bounded flush

The current stop_event prevents result delivery: device/hermeshands_device/loop.py:172-206 checks stop_event before attempting each result send and waits on it before raising. Replace that binary behavior with a lifecycle state protected by a lock: RUNNING, STOPPING, STOPPED.

On S or session timeout:

1. Atomically enter STOPPING. The poll loop stops admitting work. Poll and heartbeat report stopping. The relay refuses new jobs for that device.
2. Take a stable snapshot of active job IDs. Run the verified 18B cancellation/termination path for all active process trees. Do not create a second termination implementation.
3. For each active job, queue its final result with stopped_by_person or session_timeout. Result delivery remains enabled while STOPPING. Retry with bounded backoff until a single global five-second flush deadline. Do not spend five seconds per job.
4. Refuse and return a final stopped result for any job already returned by a poll that raced with STOPPING. Never execute it.
5. After result attempts, save and sign the local receipt. Send a final heartbeat with witness=false and session_state=stopped if the gateway is reachable. Then enter STOPPED, set the process exit signal, and exit.

The relay checks persisted session_state and witness status before returning jobs, so the device-side race handling is defense in depth. A five-second flush is a bound on attempts, not a promise of delivery during an internet outage. If delivery is not confirmed, the local signed receipt records the pending result and the UI says delivery was not confirmed.

Use a result queue and a single flush deadline. Do not make the result worker exit just because stop was requested. The normal non-witness stop path must continue to work. Ensure shutdown cannot wait forever on an HTTP request by using bounded request timeouts.

### 4.3 Labels and signed receipt

For every command and prompt, write a timestamped device-side event. Use fixed labels: known F4 playbook diagnostics get their named diagnostic label; an exact known command may use its static label; all other commands are labeled “Running a command”. Also store the exact command string received and the observed result. Do not claim that a command was run if the policy refused it.

Write a local JSON receipt on session end. Its payload contains schema_version, device_id, device name, paired owner display label, gateway host, session ID, start and end timestamps, end_reason, event list, observed checkup facts, and pending delivery statuses. Event fields include timestamp, event type, fixed label, exact observed command where applicable, outcome enum, returncode, and error code. Include device public-key fingerprint and a signature over the canonical JSON payload using the registered device private key. Keep the signature outside the signed payload. Verify the receipt with the paired public key in tests.

The receipt describes observations and outcomes, not claims of changed state. A repair is “fixed” only after a named recheck observes the desired state. Otherwise state “not fixed” or “unknown”. Never include an inferred cause in the facts list. Keep the receipt on the device, with local user-readable permissions. Do not upload it automatically. Avoid printing command output or credentials into the receipt unless already observed and necessary to establish the outcome; redact known secret-bearing environment fields. These limits must be tested.

### 4.4 Checkup facts versus possible causes

The checkup enumerates facts with source and availability. Examples: free disk bytes from the OS API; memory pressure; process names and observed resource values; Wi-Fi interface state; DNS lookup result; fixed reachability probe result; print queue status; reboot/update indicators; battery API result; startup count. Do not report personal process command-line arguments.

A separate deterministic formatter may say “Possible cause” only with a link to fact IDs and a confidence label. It may not infer a definite cause from correlation. No LLM-authored arbitrary diagnostics or repairs are part of the checkup contract. Thresholds are constants in the checkup implementation and each boundary receives a unit test.

## 5. F2 approval hook and target security

Register a pre_tool_call hook for terminal, write_file, and patch. Verify exact registered tool names in the Hermes source: hermes_cli/tools/terminal_tool.py:1618 and 1751-1755; hermes_cli/tools/file_tools.py:1411 and 1434. For a witness target, every write_file and patch call requires approval. terminal calls require approval unless they match the exact read-only allowlist. A proposed repair or any unrecognized terminal command requires approval before execution.
The hook must resolve the target exactly as the Hands environment does. The environment calls targets.resolve_target using the active HERMES_SESSION_KEY, current cron execution, and default target at hermes-hands/hands/environment.py:330-343. Reuse hermes-hands/hands/targets.py:29-47 and the same active session context. Do not accept a device ID, device name, or session ID from tool arguments as an alternate target selector. Check that the active owner is authorized for the resolved device. If the resolved target is server or no device is selected, the Hands witness rule does not apply.

Use a narrow, versioned, read-only command manifest shared by the checkup formatter and F4 diagnostics. Match a parsed executable and exact argument vector, not a substring or broad command prefix. Reject shell metacharacters, command chaining, redirection, script execution, and unknown arguments from the allowlist. File writes and patches are never on the allowlist. Preserve the Hermes core approval behavior for commands it independently marks dangerous.

For every approval request, pass a per-call key such as hands_witness:<device_id>:<tool_call_id> to request_tool_approval. The API and rule_key are at hermes_cli/tools/approval.py:1208-1233. A grant must apply to that call only and must not create a device-wide or future-session allow rule. Test approval, denial, timeout, duplicate calls, and two devices in different sessions.

The hook runs in the gateway's active session context. Hermes binds session source and session key in gateway/run.py:3959-3989 and exposes them through gateway/session_context.py:188-194. Use those values for owner checks and target resolution.

Important failure behavior: hermes_cli/agent/tool_executor.py:661-675 catches an unexpected hook exception and proceeds with the original arguments. The hook must have a top-level exception boundary that returns an explicit block decision for a protected witness action whenever resolution, authorization, or approval fails. It must avoid network calls and keep the callback small. However, do not claim that this hook alone guarantees fail-closed behavior against every interpreter-level or dispatch failure. Retain and test Hermes core approval protections; report this boundary truthfully in documentation and the risk register. For any failure where the hook cannot determine whether the selected target is a witness device, do not claim a block that has not actually been enforced.

## 6. F7 owner-chat route and source seams

The dashboard process owns the relay, but the plugin callback is not the gateway injection host. Do not call ctx.inject_message from the dashboard process. Hermes defines it at hermes_cli/plugins.py:588-595 and delegates only when gateway injection is enabled. Gateway injection permission is checked at plugins.py:624-632 and hermes_cli/plugins_injection.py:33-65. The relevant source object is SessionSource in hermes_cli/gateway/session.py:69-168. Use its to_dict shape exactly; do not invent an origin mapping.

At /hands use, persist the owner's current SessionSource.to_dict() and owner principal with the selected device. The command is owner-authorized and direct-message-only at hermes-hands/hands/commands.py:247-313. Pairing can retain a display label, but it cannot substitute for the actual chat origin. /hands use is the authoritative origin update because it runs in the chat where the owner chose the device. Add an additive owner_origin_json field to the device/session store row and validate platform, chat ID, chat type, and user ID before use.

Add a small gateway-side background poller registered by the plugin. The dashboard stores signed help requests in the shared plugin database; the database is already described as shared by gateway and dashboard in hermes-hands/hands/store.py:1-5. The gateway poller claims pending rows with a short lease, verifies owner origin and that the live gateway injection capability exists, and calls ctx.inject_message(content, origin=stored_session_source). Do not use ctx.post_message: Hermes documents it as delivering only into the session bound to the current tool call at plugins.py:607-611.

The injected message is a concise device-originated request, for example: “Mum’s PC asks for help: my printer won’t print. Reply here to help.” Add a stable request reference for deduplication. Mark a request queued only when inject_message returns true, which means accepted/scheduled by the live gateway, not delivered to Telegram. If it returns false or raises, release the lease with bounded backoff and retain the request. Surface pending and queued status through /hands status. Do not tell the device that the owner received the message until an actual delivery receipt exists.

Register the poller only in the gateway process. Use PluginContext.on_unload to stop it; the API is at hermes_cli/plugins.py:416-423. The gateway publishes an injection scheduler when its runner starts at hermes_cli/gateway/run_plugin_injection.py:26-52 and gateway/run_startup.py:1756. Verify gateway capability before claiming rows. The available manager capability seam is hermes_cli/plugins.py:1166-1182; because this is version-sensitive, exercise it against the exact installed Hermes release. Add plugins.entries.hermes-hands.allow_gateway_injection: true to setup documentation. Without it, retain requests as pending and show the configuration requirement. Do not bypass the permission check.

The relay routes are registered in hermes-hands/dashboard/plugin_api.py:110-122; add the help endpoint there and in hermes-hands/hands/relay.py:32-44. Use an at-least-once outbox with request IDs. The gateway may crash after injection succeeds but before marking the row queued, so a duplicate is possible. Keep the stable reference visible and do not claim exactly-once delivery.

## 7. F4 playbooks

Ship all five playbooks in the Hands skill and test them as content:

1. No internet.
2. Printer stuck.
3. Slow PC.
4. Disk full.5. Program frozen.

Each follows the same sequence: confirm symptom; run read-only diagnostic commands from the exact allowlist; report facts and uncertainty; propose one reversible repair; state any UAC/admin prompt before running it; wait for F2 approval; execute only the approved repair; run an explicit recheck; report fixed, not fixed, or unknown based on observations. Never hide an elevation prompt or present a recommendation as an executed change.

Before publishing the skill text, run every diagnostic and repair step on the live Windows laptop as a standard user. Record which commands need UAC, require administrator rights, vary by Windows version, or are unsupported on Linux. Ensure decline paths are tested. F6 Linux parity means the device and fixed checkup work in WSL; it does not imply that Windows repair playbooks run on Linux. The documentation must say which playbooks are Windows-only.

## 8. Parallel build lanes and ownership

The two Codex lanes are disjoint. Do not edit the other lane's files. Integrate through the wire contract in section 3. The first task in the device lane is to review and validate the existing 18B diff.

### Lane A: device, Codex

Owns only:

- device/hermeshands_device/executor.py
- device/hermeshands_device/loop.py
- device/hermeshands_device/cli.py
- device/hermeshands_device/console.py, new
- device/hermeshands_device/checkup.py, new
- device/tests/test_executor.py
- device/tests/test_loop.py
- device/tests/test_config_and_cli.py
- device/tests/test_console.py, new
- device/tests/test_checkup.py, new
- device/tests/test_keys_and_screen.py

Write or extend tests for: 18B revoke process-tree cleanup and PID reuse; witness TTY gate and startup consent; session-start screenshot consent; one console input owner and no blocked poll loop; screenshot Y/N and 30-second timeout; S while idle, during execution, during screenshot prompt, and while editing H; stopping state and the global five-second flush bound; stop races with newly polled jobs; timeout at the 30-minute boundary and after sleep; unknown job type refusal; checkup schema, partial/unsupported fields and thresholds; H message validation; receipt signature verification, end reasons, observed outcomes, and redaction.

### Lane B: gateway, Codex

Owns only:

- hermes-hands/__init__.py
- hermes-hands/hands/relay.py
- hermes-hands/hands/store.py
- hermes-hands/hands/commands.py
- hermes-hands/hands/client.py
- hermes-hands/hands/tools.py
- hermes-hands/hands/witness.py, new
- hermes-hands/dashboard/plugin_api.py
- hermes-hands/plugin.yaml
- hermes-hands/README.md
- hermes-hands/skills/hands/SKILL.md
- hermes-hands/tests/test_store.py
- hermes-hands/tests/test_relay.py
- hermes-hands/tests/test_commands.py
- hermes-hands/tests/test_tools.py
- hermes-hands/tests/test_plugin_api.py
- hermes-hands/tests/test_e2e_device_relay.py
- hermes-hands/tests/test_desktop_and_skill.py

Write or extend tests for: additive migrations; witness authenticated and persisted on both poll and heartbeat; strict field validation; capability gating; checkup enqueue and typed result round trip; unknown type rejected at gateway; stopping/stopped devices receive no new work; help body limits, request signature, duplicate IDs, rate limit, revoke rejection; origin save and owner scoping; gateway-only injection with permission; live gateway missing and false injector outcomes; poller lease, retry, unload, and duplicate window; hook target parity with environment; exact allowlist parsing; per-call approval key; approval deny and timeout; hook exception returning an explicit block; output for facts versus inferences; all five playbooks present and diagnostics-first. Extend hermes-hands/tests/test_e2e_device_relay.py for signed device poll, checkup, screenshot consent/denial, H request through the shared store and simulated gateway injection, Stop with a live job, stopped result delivery, and refusal of new jobs.

### Lane C: Hermes worker, after Monday 11:00 WAT

Use the worker as an independent reviewer, not a third editor. It owns no files and must not overwrite either Codex lane. Ask it to review the exact Hermes hook and injection seams, the read-only allowlist, approval key scope, SessionSource origin, gateway poller lifecycle, F4 commands, and the complete diff against the plan. It reports blocking issues with file and line references. Codex owns fixes in its lane. The reviewer runs no separate test suite; each implementation lane owns its tests.

## 9. Test and release gates

Run after code is present. The complete repository test command from ROUND2-COMMON.md is:

    .venv\Scripts\python -m pytest hermes-hands/tests device/tests -q -p no:cacheprovider

Also run the plugin tests against the supplied Hermes tree using the documented PYTHONPATH and hvenv interpreter. Before treating that result as authoritative, establish that the source tree matches the installed Hermes release. Run the device package's own test and type/lint commands already defined by its project configuration. Record exact commands and final summaries in the PR notes.

### Unit gates

- 18B: revoke stops the full process tree, handles WindowsApps python3 alias children, protects against PID reuse using creation time, and stays within the revoke timing target.
- F1 and F5: consent is explicit; no TTY refuses witness mode; screenshot permission is per session and per request; every timeout defaults to No.
- F2: same target resolution as environment; unknown or malformed shell commands are not allowlisted; repairs and file writes require approval; the key is unique per tool call.
- F3: fixed checkup only; facts include source/status/availability; unsupported fields are explicit; inferences cite facts and remain separate.
- F4: all five playbooks exist, diagnostics come first, repairs have approval and a recheck.
- F6: device protocol and checkup run on Windows and Linux; platform-specific unsupported results are represented honestly.- F7: signed request validation, owner origin shape, owner authorization, deduplication, gateway permission, and pending retry behavior.
- F8: monotonic 30-minute deadline, wake-up expiry check, timeout result, receipt end reason.
- Receipt: signature verifies against paired device key; command outcomes reflect observed results; no unobserved state-change claims.

### End-to-end gates

Extend hermes-hands/tests/test_e2e_device_relay.py. Keep the existing pair, policy-change, revoke, cancel, and device-signature coverage. Add complete paths for all eight features, including two devices with different owners to prove origin isolation. Test gateway injection disabled and enabled. Verify signature/body binding for witness, checkup, and help. Test relay outage during Stop and assert a local signed receipt with delivery not confirmed.

### Live gates after Monday 11:00

1. Windows laptop: pair, start witness consent, inspect displayed limits, run checkup, exercise each playbook's diagnostic and repair approval path, decline one repair, decline a screenshot, test screenshot Y then N on a separate request, send H, stop with an active job, inspect local receipt and verify its signature.
2. WSL Linux device: pair a Linux device over the same protocol, run witness consent in an interactive terminal, check the Linux checkup facts and unsupported fields, send H, stop, verify receipt. Run the unattended service path separately and prove witness mode refuses it.
3. VPS internet path: keep the device laptop on a different network, connect the gateway to the VPS, and repeat heartbeat, poll, checkup, H, screenshot consent, Stop, and reconnect/retry behavior. Include the bounded-flush outage case.
4. Telegram: confirm the saved owner origin delivers the request into the correct owner's Telegram chat only when gateway injection is enabled. Verify another user's chat receives nothing. Confirm status text says queued only after the gateway accepts injection.
5. CI: run GitHub CI and verify the configured checks. The design review found the existing CI may be affected by owner billing or repository visibility. Confirm those external prerequisites early; if CI cannot run, report the exact reason and do not mark the CI gate passed.
6. macOS disclosure: retain the design's macOS caveat and state that this release's live parity was verified on Windows and WSL Linux only unless macOS is actually tested.

### Per-feature acceptance gates

- F1: explicit startup consent; visible owner, gateway and capabilities; foreground-only; no input control; signed receipt.
- F2: owner-scoped target parity; exact read-only allowlist; per-call approval; clear treatment of hook failure and core approval.
- F3: fixed checkup returns typed structured facts and separate inferences.
- F4: five tested playbooks, with approval and recheck for every repair.
- F5: session screenshot permission plus individual Y/N prompt; timeout is No; prompt does not interrupt poll or heartbeat.
- F6: WSL Linux device works through VPS gateway; GitHub CI status recorded; macOS support stated accurately.
- F7: H request reaches the paired owner's chat through a gateway-side poller; no cross-owner delivery; retry and deduplication behavior is visible.
- F8: 30-minute absolute timeout stops work, attempts bounded flush, and records session_timeout.
- 18B prerequisite: revoke still terminates all descendants before F1 or Stop is accepted.

## 10. Risk register

| Risk | Mitigation |
|---|---|
| The uncommitted 18B fix regresses or has an untested descendant path. | Treat review of the exact worktree diff and the focused revoke tests as gate zero. Stop reuses only the verified termination implementation. |
| Stop is requested while an HTTP result post is blocked. | Use bounded HTTP timeouts, keep result delivery alive in STOPPING, one global five-second deadline, and a signed local receipt when remote delivery cannot be confirmed. |
| A poll response races with Stop. | State transition under a lock, relay state check, and explicit rejection result for any job received after STOPPING. |
| Hook errors are fail-open in Hermes tool_executor. | Catch expected failures inside the hook and return an explicit block for protected actions. Keep core approval enabled and independently tested. Never describe the hook as an absolute fail-closed boundary. |
| Allowlist parser accepts a write hidden in shell syntax. | Match exact parsed executable plus argument vector; deny shell composition, redirects, scripts, unknown flags, and all write tools. Test bypass-shaped inputs. |
| A target mismatch approves or blocks the wrong device. | Reuse the environment resolver and active session context; derive target only from the owner-scoped session binding; test two owners and server-only sessions. |
| Checkup reports a guess as a fact or leaks personal process data. | Typed facts with sources and availability; separate fact-linked inferences; omit process command lines; test schema and redaction. |
| Windows repair needs UAC or causes a change different from its label. | Run each F4 step on the live laptop as a standard user before publishing; identify UAC before approval; require a post-change recheck; report unknown when unconfirmed. |
| Console prompt blocks poll, heartbeat, or Stop. | One console owner outside poll threads; tests hold each prompt open while asserting heartbeat and poll continue. |
| Witness mode starts unattended through service or pythonw. | Require a TTY and reject the service path explicitly; test both entry points. |
| Device sleeps through the session deadline. | Use an absolute monotonic deadline and check it before work on every wake. |
| Help is delivered to the wrong Telegram conversation. | Persist validated SessionSource from owner-authorized /hands use; gateway-only injection; verify owner and two-user isolation end to end. || Gateway crashes between injection acceptance and row update. | At-least-once outbox, stable request reference, dedupe marker in the message, lease/retry, and status wording that does not claim exactly-once delivery. |
| Hermes injection is disabled or no live gateway exists. | Check allow_gateway_injection and live gateway capability before claiming work; keep requests pending and surface the missing setup. |
| The supplied Hermes source is not the exact release. | Compare it to installed Hermes or an exact tagged checkout before coding the hook; rerun seam tests against the release used by the owner. |
| Linux or WSL lacks a Windows-only diagnostic API. | Return unsupported or error per field, keep Linux device parity, and label Windows-only playbooks accurately. |
| Receipt stores sensitive command text. | Keep the receipt local, restrict file permissions, avoid command output and environment secrets, redact known secret-bearing fields, and make no automatic upload. |
| Build time is short for all eight features and live tests. | Work in the two disjoint lanes concurrently; keep F4 commands fixed and narrow; start with 18B; stop new scope at Monday 14:00; reserve Monday afternoon for the specified test matrix. No feature is removed to meet the date. |
| GitHub CI cannot run because of billing or repository visibility. | Confirm the owner-controlled prerequisite before the live window, capture the actual status, and report an external blocker without marking the gate passed. |

## 11. Hour-by-hour build timeline

All times are WAT. The timeline starts Sunday, November 8, at 18:00 and reaches the Portal reset Monday, November 9, at 11:00. The two Codex lanes work concurrently in their separate file sets.

| Time | Device lane | Gateway lane |
|---|---|---|
| Sun 18:00-19:00 | Inspect exact 18B diff; map revoke tests and process-tree behavior. Freeze the signed wire fields with lane B. | Verify store, relay, target, and Hermes source seams; compare Hermes source identity; define additive schema and approval contract. |
| 19:00-20:00 | Run focused 18B test gate; correct only 18B regressions before new work. Implement lifecycle state and console thread skeleton. | Add store migrations for witness state, capabilities, owner origin, and help outbox. |
| 20:00-21:00 | Add foreground-only witness CLI and startup consent screen. | Extend poll and heartbeat validation, signed witness persistence, and capability gating. |
| 21:00-22:00 | Add separate session screenshot permission and prompt queue. | Add checkup job enqueue and typed final result storage/output path. |
| 22:00-23:00 | Implement STOPPING cancellation through the 18B process-tree path and bounded result delivery. | Reject new jobs for stopping/stopped devices; add stopped result mapping and explicit unsupported job rejection. |
| 23:00-00:00 | Add per-request screenshot prompt, timeout-to-No, and prompt IDs. | Add signed help endpoint, input validation, rate limit, and duplicate request handling. |
| Mon 00:00-01:00 | Add fixed checkup facts and bounded platform probes. | Persist owner origin from /hands use and owner principal. |
| 01:00-02:00 | Add checkup schema tests and explicit unknown job refusal. | Add gateway-only poller, lease/retry, unload stop, and permission/capability checks. |
| 02:00-03:00 | Add H editor and signed help request client with text validation. | Add pre_tool_call hook using environment target resolution and owner scope. |
| 03:00-04:00 | Add absolute timeout and wake-up deadline checks. | Add exact read-only allowlist and per-call approval key; preserve core approval. |
| 04:00-05:00 | Add signed receipt payload, outcomes, and end reasons. | Add checkup facts/inferences formatter and end-to-end relay behavior. |
| 05:00-06:00 | Test receipt verification, prompt concurrency, stop races, and partial flush. | Test injection disabled/enabled, two-owner source isolation, hook error block, and approval decisions. |
| 06:00-07:00 | Finish Linux-compatible device paths and WSL-specific unsupported values. | Write all five F4 playbooks and content tests; identify live command validation checklist. |
| 07:00-08:00 | Run device suite and fix device-lane failures. | Extend e2e and plugin API/store/relay tests; fix gateway-lane failures. |
| 08:00-09:00 | Run combined signed-contract scenarios against local relay fixtures. | Run combined signed-contract scenarios against local device fixtures. |
| 09:00-10:00 | Review device diff against every F1, F3, F5, F6, F7, F8 gate and 18B. | Review gateway diff against every F2, F3, F4, F6, F7, F8 gate. |
| 10:00-11:00 | Freeze candidate, ensure 18B work is included, and record remaining risks. | Freeze candidate, verify docs and migrations, prepare tests and launch instructions for the worker review. |

## 12. Post-reset execution to PR deadline

- Monday 11:00-12:00: Hermes worker reviews the exact release-specific hook and gateway injection seams. Codex runs the full unit suites in parallel and resolves findings.
- 12:00-13:00: Run extended test_e2e_device_relay.py and the full repository test command. Fix integration defects only.
- 13:00-14:00: Live Windows laptop session: consent, checkup, each F4 diagnostic and approval/decline path, screenshot denial and grant, H message, Stop during active work, receipt verification. Freeze feature scope at 14:00.
- 14:00-15:00: WSL Linux interactive device parity, checkup partial fields, H route, Stop, receipt, and refusal in unattended service mode.
- 15:00-17:00: VPS internet run with the laptop on a different network. Exercise Telegram owner routing, request retry, disconnect during Stop, and bounded flush.
- 17:00-18:00: GitHub CI and release checks. Record actual external billing/repository blockers if present.
- 18:00-20:00: Regression fixes limited to release-blocking defects, followed by rerunning affected gates. No new feature scope.- 20:00-21:00: Review the whole diff, verify version and setup docs, record exact test commands and results, prepare PR summary.
- 21:00-22:00: Owner reviews the concrete PR artifact. Submit only after explicit owner approval. This plan authorizes preparation, not submission.
- Tuesday 14:00: Freeze the pin after the owner's review and the final verified build.

## 13. Completion definition

v0.2 is ready for PR only when every per-feature acceptance gate passes; the 18B revoke prerequisite remains green; the Windows, WSL Linux, VPS internet, Telegram, and CI outcomes are recorded; the source release identity is verified; the signed receipt matches observed events; and remaining limitations, including the Hermes hook exception boundary and at-least-once help delivery, are stated accurately. No feature F1 through F8 is removed to reach the deadline.