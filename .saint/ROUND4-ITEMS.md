# Round 4 items for the docs, CI and doctor lane

Extracted from the 340-agent re-review of 1586b18. Each item was confirmed 2 of 3 by adversarial checks.

## R2 [blocker] (new_defect, votes 2/3) judge-and-gate:install-routes-404: Both documented install routes fail today: the GitHub repo and the PyPI name `hermes-hands` return 404, the PyPI name is unclaimed, and the README has no fallback
File: README.md:11

Quick start step 1 (`hermes plugins install Ridwannurudeen/hermes-hands/hermes-hands --enable`, README:11-12 and :39) needs a public repo. The device steps (`pipx install hermes-hands==0.1.0`, README:85-89 and :100, and the UPGRADE text in hermes-hands/hands/messages.py:5) need the package on PyPI. Neither is live, so a judge cannot install either half. The README admits the repo is private only in the Tests section (README:170), and it says nothing about PyPI. The name is unclaimed, so whoever publishes `hermes-hands` 0.1.0 first gets their code installed as the laptop agent by the pinned command. This is the owner's planned release step (PLAN-v3 §6, §11), and it must land before the PR is opened. PLAN-v3 §13 promised a git+sha fallback that the README does not have. The two reviewers rated this major and blocker. It is kept as a blocker because a judge cannot complete the flow at all.

Fix: Owner: reserve the PyPI name now, make the repo public and publish 0.1.0 from the tag, all before opening the PR. Until then, add `pipx install "git+https://github.com/Ridwannurudeen/hermes-hands@v0.1.0#subdirectory=device"` as a fallback and a one-line note at Quick start that the repo and the PyPI release go live on the tag.

Evidence: `curl -s -o /dev/null -w "%{http_code}" https://pypi.org/pypi/hermes-hands/json` prints 404. `curl ... https://github.com/Ridwannurudeen/hermes-hands` prints 404 (run 2026-11-08). device/pyproject.toml:6 has `name = "hermes-hands"`. PLAN-v3 §6: 'PyPI account, reserve the name (free as of today) and later publish from the tag'. PLAN-v3 §13 PyPI row: 'git+sha fallback'.

## R4 [major] (new_defect, votes 2/3) claims-and-admission:file-ops-cache-writes-rule9: The plugin writes into core's private file-tool cache dicts, which rule 9 forbids, and disclosing it does not satisfy the rule
File: hermes-hands/hands/environment.py:314-327

_forget_file_ops takes tools.file_tools._file_ops_lock, iterates tools.file_tools._file_ops_cache, and clears the private _command_cache, _rg_resolution_cache and _rg_modified_capability on each core ShellFileOperations object. Rule 9 says a listed plugin must not replace, wrap or rebind core private dicts in place (writes into a core module's tables). The submission guide treats rule 9 problems as declines rather than bug-fix requests. The validator does not flag attribute writes, but the PR itself points reviewers at these lines. This is the V2/C27 coupling: round 3 only disclosed it and moved the lines.

Fix: Remove the writes into core state, for example by giving each target a distinct env object so file tools build a fresh ShellFileOperations. Otherwise accept the stale cache as a disclosed known limit until core adds the invalidate_file_ops seam (hook ask 7).

Evidence: environment.py:322 'with file_tools._file_ops_lock:'; :323 'stale = [ops for ops in file_tools._file_ops_cache.values() if ops.env is self]'; :325-327 'ops._command_cache.clear()', 'ops._rg_resolution_cache.clear()', 'ops._rg_modified_capability.clear()'. The rule text is in evidence/catalog-submission.md, rule 9 and 'Opening the PR' step 4.

## R5 [major] (new_defect, votes 3/3) claims-and-admission:readme-double-slash-bypasses-approval: README tells users to double the slash (cmd //c, tasklist //FO), which skips Hermes approval, and wrongly says the bundled skill gives the same advice
File: README.md:116

README.md:116 says native Windows /switch commands need the slash doubled, as in `cmd //c dir` or `tasklist //FO CSV`, and that the bundled hands skill tells the model this. Both parts are false at HEAD. In 85b6268 SKILL.md:41-49 was changed to the opposite advice: use MSYS_NO_PATHCONV=1 with single slashes, and never double the slash, because that hides the command from approval. README:116 was not updated. The doubled form gets past v0.23.0's dangerous-command detection, so the README (the rendered catalog page) teaches users the spelling that bypasses approval, while README:5 and plugin.yaml promise that approvals run before device commands.

Fix: Rewrite README:116 to match SKILL.md:41-49: use MSYS_NO_PATHCONV=1 with single-slash switches, never doubled slashes, and say why.

Evidence: Under real Hermes v0.23.0, tools.approval_detection.detect_dangerous_command (review3-scratch/claims/slash.py and review3-scratch/claims-admission/approve.py) returns: 'taskkill /F /IM explorer.exe' -> (True, 'force kill processes (taskkill /F)'); 'taskkill //F //IM explorer.exe' -> (False, None, None); 'MSYS_NO_PATHCONV=1 taskkill /F ...' -> True; 'cmd /c rd /s /q C:/x' -> (True, 'Windows cmd destructive delete'); 'cmd //c rd //s //q C:/x' -> (False, None, None); 'cmd //c del //s //q C:/tmp' -> (False, None, None). The patterns that match only a single slash are at tools/approval_detection.py:433 and :467. `git log -S MSYS_NO_PATHCONV` shows only 85b6268 and db1a036 touching SKILL.md.

## R6 [major] (new_defect, votes 3/3) claims-and-admission:validator-seven-private-imports: PR and hook issue say the validator flags six private core imports, but at HEAD it flags seven, and the new one reads the profile .env without disclosure
File: hermes-hands/hands/commands.py:28-35

Round 3 (85b6268, V49) added terminal_backend(), which imports tools.terminal_scope.build_profile_terminal_scope (commands.py:32). That function loads the profile's whole .env through load_env_file and keeps the TERMINAL_* keys (tools/terminal_scope.py:138-151). It is also handed live os.environ TERMINAL_* as env_overlay, which its docstring says must be a trusted mapping captured before multiplexing (terminal_scope.py:124-129). PR-DESCRIPTION.md ('Validator warnings and hook request') and HOOK-REQUEST-ISSUE.md still name six imports and ask for no seam for this one. The PR's claim of 'only the private import warning above' does not match what catalog CI prints. Rule 9 reviewers read warnings by file and line, and rule 11 says a plugin never reads .env files, so a reviewer would find this undisclosed indirect .env read and, under rule 13, request changes.

Fix: List build_profile_terminal_scope (commands.py:32) in the PR validator paragraph, the surfaces table and the hook issue, and add a seam ask for a public read of the effective terminal backend. Disclose the indirect .env read, or read terminal.backend through load_config_readonly and TERMINAL_ENV through get_secret instead. Paste the exact validator output at the pin into the PR.

Evidence: hermes_cli.plugin_validate.validate_plugin_dir run at HEAD 1586b18 under real Hermes v0.23.0 (review3-scratch/claims/val_doc.py; output in review3-scratch/claims-admission/val.json) gives ok=true with warnings for: __init__.py:12 hermes_cli.dashboard_auth.token_auth.register_token_route; hands/commands.py:32 tools.terminal_scope.build_profile_terminal_scope; hands/environment.py:25 tools.environments.local.LocalEnvironment; hands/environment.py:456 tools.approval.get_current_session_key; hands/environment.py:457 tools.terminal_tool._get_env_config, get_session_cwd, resolve_task_overrides. That is 7 names. `git log -S build_profile_terminal_scope -- hermes-hands/hands/commands.py` -> 85b6268.

## R16 [major] (new_defect, votes 3/3) judge-and-gate:readme-doubled-slash: README still teaches the doubled slash (`cmd //c`, `tasklist //FO`) that SKILL.md now forbids as an approval bypass, and it falsely says the skill teaches it (V23 doc half still open)
File: README.md:116

README:116 says that native Windows commands 'need the slash doubled, as in `cmd //c dir` or `tasklist //FO CSV`' and that 'The bundled `hands` skill tells the model this.' Round 3 rewrote SKILL.md:42-48 to say the opposite: 'Never double the slash (`//c`, `//F`): that spelling hides the command from Hermes's approval check', and it uses `MSYS_NO_PATHCONV=1` instead. So the README misstates what the skill says, and it teaches humans the spelling V23 identified as the way past dangerous-command approval (taskkill //F, rd //s //q).

Fix: Replace README:116 with the SKILL's rule: `MSYS_NO_PATHCONV=1` in front of the program, single-slash switches, Windows-form quoted paths for that program, and never `//`.

Evidence: `grep -n '//c\|//FO\|doubled\|MSYS_NO_PATHCONV' README.md hermes-hands/skills/hands/SKILL.md` gives README.md:116 'need the slash doubled, as in `cmd //c dir` or `tasklist //FO CSV` ... The bundled `hands` skill tells the model this.' and SKILL.md:44-47 'program with `MSYS_NO_PATHCONV=1` in front ... Never double the slash (`//c`, `//F`): that spelling hides the command from Hermes's approval check'. The V23 bypass is in ROUND2-VERIFY.md:203. ROUND3-LANES.md, Opus C lane, V23: 'Rewrite it so destructive /switch commands are never suggested'.

## R17 [major] (new_defect, votes 3/3) judge-and-gate:readme-test-claims-stale: README test claims are false at HEAD: it says 180 tests with 178 passing on Windows, but HEAD has 307 tests and at least 6 fail on Windows
File: README.md:170

README:170 says '180 tests ... 126 plugin tests, 6 of them end to end ... and 54 device tests. On Windows 178 pass and 2 are skipped by design.' At HEAD the plugin suite collects 199 tests (12 of them e2e) and the device suite collects 108, for 307 in all. On Windows with real Hermes 0.23.0, 4 e2e tests and 2 device executor tests fail because of the taskkill regression. The Windows CI leg the section describes would also go red.

Fix: After the taskkill fix, rerun both suites on Windows and Linux, then rewrite README:170 and the PR text with the real counts and the real pass and skip numbers.

Evidence: `PYTHONPATH=<hermes-v0.23.0> hvenv/Scripts/python -m pytest --collect-only -q hermes-hands/tests` gives '199 tests collected'. In device/, `pytest --collect-only -q tests` gives '108 tests collected'. `grep -c "def test_" hermes-hands/tests/test_e2e_device_relay.py` gives 12. The runs gave '4 failed, 35 passed' (e2e, commands, cli) and '2 failed, 19 passed, 2 skipped' (test_executor.py).

## R26 [major] (unfixed, votes 3/3) C3: C3: repro=partial, trace=partial
File: docs-ci-catalog:

[repro:partial] residual: Neither workflow has ever run (no remote; GitHub 404). The README:170 and PR:56 test counts (180) are stale, against 307 collected. Gate 2 item 8 (CI green) is unmet.
[trace:partial] residual: Push, get both legs green, fix the timing-fragile test, and refresh the counts.

Fix: 

Evidence: [repro] Same as V1. The working-directory is now hermes-hands/hermes-hands (tests.yml:76, 85). The venv is on GITHUB_PATH (:73). mss comes in through `-e ./hermes-hands/device` (:72). Run locally at HEAD: validate_plugin_dir ok=true, doctor OK, footgun rc 0 (52 files), catalog shape OK, payload scan rc 0, and 307 tests collected with no errors.
[trace] Same state as V1. The structural fixes hold: working-directory is hermes-hands/hermes-hands (tests.yml:40, 49), the venv goes onto GITHUB_PATH (:37), and the install includes the device package (:36). But CI has never run (no git remote). README:170 and PR:56 cite 180 tests while 307 are collected at HEAD. On Windows, device/tests/test_executor.py::test_chatty_background_writer_is_bounded_after_shell_exit fails 3 of 3 runs here because of its 0.4 s timeout (python cold start is 0.786 s), and that test is in the CI pytest step.

## R28 [major] (unfixed, votes 3/3) C7: C7: repro=partial, trace=partial
File: docs-ci-catalog:

[repro:partial] residual: README.md:116 contradicts SKILL.md. There is no device test for `cmd /c` with the opt-out (see V61).
[trace:partial] residual: Fix README:116, add a device test, and add a /switch cue to the skill description.

Fix: 

Evidence: [repro] Device bash still converts /switches (by design). SKILL.md:42-49 now tells the model to run native /switch programs as `MSYS_NO_PATHCONV=1 <cmd> /switch`, and the repro (v23_skill_forms.py through executor.run) shows those forms work: 'MSYS_NO_PATHCONV=1 cmd /c echo hi-optout' gives rc 0 'hi-optout', and tasklist /FO CSV and ipconfig /all give rc 0. But README.md:116, the Windows note the fix asked for, still gives the doubled-slash advice that contradicts the skill and gets past approval.
[trace] The executor still leaves path conversion on, as round 1 recommended. SKILL.md:42-49 now gives a correct and approval-safe rule, and my pathconv_skill.py repro shows that the skill forms work and that `cmd /c echo` still runs nothing. Three gaps remain. (a) README.md:116 contradicts the skill, teaches `cmd //c dir` and `tasklist //FO CSV`, and claims the skill says this. (b) No repo test covers `MSYS_NO_PATHCONV=1 cmd /c` or `cmd //c` (the git grep over the test dirs is empty). (c) The rule sits only in the on-demand skill, whose description has no /switch cue (SKILL.md:3). So a model that has not loaded the skill can still run `cmd /c del x` and get exit 0 with nothing done.

## R30 [major] (unfixed, votes 3/3) V1: V1: repro=partial, trace=partial
File: docs-ci-catalog:

[repro:partial] residual: CI has never run: `git remote -v` is empty and https://github.com/Ridwannurudeen/hermes-hands returns 404. The counts are stale: README.md:170 and PR-DESCRIPTION.md:56 say 180 (126+54), but 307 (199+108) are collected. PR:57 still holds the [FOOTGUN_RESULT_AT_PIN] placeholder. I did not run the full suites.
[trace:partial] residual: Get both legs green on a pushed branch. Fix or loosen the 0.4 s timeout in test_chatty_background_writer_is_bounded_after_shell_exit. Refresh the test counts in README:170 and PR:56, and fill in PR:57.

Fix: 

Evidence: [repro] HEAD 1586b18 exported with `git archive` to review3-scratch/docs-fixcheck/ws. I ran each tests.yml step myself. (1) The install line, tests.yml:72, now has `-e ./hermes-hands/device`, and device/pyproject.toml pulls mss>=9,<11. With mss present (hvenv), `pytest tests ../device/tests --collect-only` from hermes-hands/hermes-hands collected 307 tests with no collection errors (199 plugin, 108 device). (2) `check-windows-footguns.py --all` printed 'No Windows footguns found (52 file(s) scanned)', rc 0. (3) `python -m hermes_cli.main` cannot start in hvenv: python-dotenv is only declared for python_version>=3.14 (hermes pyproject.toml:49) and hvenv is 3.12. So I called core's functions directly: validate_plugin_dir gave ok=true, cmd_plugin_doctor ci=True gave 'OK ... 2 tool(s)', validate_plugin_catalog.py on the CI heredoc printed 'OK: 1 file(s) valid', and scan_plugin_payloads.py returned rc 0. Hermes requires-python is '>=3.11,<3.15' (pyproject.toml:15), so the 3.14 pin at tests.yml:65 is valid.
[trace] The two named CI breaks are fixed. tests.yml:36 now installs `-e ./hermes-hands/device`, which pulls mss through device/pyproject.toml:12. `check-windows-footguns.py --all` at HEAD prints 'No Windows footguns found (52 file(s) scanned)' with rc=0. Three things still fail. (1) The repo has no remote (`git remote | wc -l` gives 0), so CI has never run. (2) README.md:170 and PR-DESCRIPTION.md:56 still say '180 tests: 126 plugin, 6 end to end, 54 device'. With hvenv at HEAD I collected 307: 199 in tests, 12 in test_e2e_device_relay.py and 108 in device/tests. PR:57 still holds the placeholder `[FOOTGUN_RESULT_AT_PIN]`. (3) The Windows leg runs `pytest tests ../device/tests` (tests.yml:53), and one of its tests fails deterministically here. device/tests/test_executor.py::test_chatty_background_writer_is_bounded_after_shell_exit failed 3 of 3 runs at HEAD with `assert b'tick' in b'parent-exit\n[device timeout]\n'`. The cause is that the test gives `python` 0.4 s (test_executor.py:149), and in Git Bash `time python -c ...` takes 0.786 s here.

## R37 [major] (unfixed, votes 3/3) V2: V2: repro=not_fixed, trace=partial
File: docs-ci-catalog:

[repro:not_fixed] residual: Add build_profile_terminal_scope to the PR table, PR:51 and the hook issue (count seven). Regenerate every cite from the pinned SHA.
[trace:partial] residual: Disclose tools.terminal_scope.build_profile_terminal_scope, change 'six' to 'seven' (PR:51, HOOK:9), add a seam ask for it, and regenerate every cite from the pinned SHA.

Fix: 

Evidence: [repro] run_validate.py (validate_plugin_dir, real v0.23.0) at HEAD gives ok=true with one warning listing SEVEN private imports. One is new: hands/commands.py:32 tools.terminal_scope.build_profile_terminal_scope, added in 85b6268 for the backend read. PR-DESCRIPTION.md:51 and HOOK-REQUEST-ISSUE.md:9 still say six. This import is in neither the surfaces table nor the hook issue, which has no ask for a public terminal-backend read. The cites are stale again at HEAD: PR:30 and HOOK:21 cite environment.py:444-445 (now 456-457); PR:31 and HOOK:23 cite 288-301 (now 322-327); HOOK:19 cites 322-330 (now 356); PR:14 cites commands.py:130 (now 148); PR:15 cites :124 (now 142); PR:17 cites environment.py:278 (now 304); PR:20 cites cli.py:115 (now 117); PR:21 cites cli.py:75 (now 80). This is the same failure mode as round 2: the validator evidence in the PR is false at the pin.
[trace] The two round-2 private imports and the file_tools cache access are now disclosed (PR:29-31 and :51, HOOK asks 6 and 7). However, running validate_plugin_dir on a git archive of 1586b18 (review3-scratch/docs-trace/val.json) lists SEVEN private imports. The added one is `hands/commands.py:32 tools.terminal_scope.build_profile_terminal_scope`. PR:51 and HOOK:9 both say 'six' and never mention terminal_scope, and no seam ask covers it. Line cites that are stale at HEAD: PR:14 commands.py:130 (register_command is at :148); PR:15 :124 (ask_choice at :142); PR:17 environment.py:278 (current_cron_execution at :304); PR:20 cli.py:115 (register_cli_command at :117); PR:21 cli.py:75 (set_config at :80); PR:30 and HOOK ask 6 environment.py:444-445 (the imports are at :456-457); PR:31 and HOOK ask 7 :288-301 (the cache access is at :320-327).

## R39 [major] (unfixed, votes 3/3) V23: V23: repro=partial, trace=partial
File: docs-ci-catalog:

[repro:partial] residual: README.md:116 teaches the spelling that gets past approval, and its claim about the skill is false. The device policy does not fold `//` (policy.py:15 covers only schtasks/taskkill against HermesHands). No repo test asserts detection on the skill's forms.
[trace:partial] residual: Rewrite README:116 to match the skill. Optionally fold `//X` to `/X` and strip a leading MSYS_NO_PATHCONV=1 in policy.refusal. A side note on core: even the single-slash `MSYS_NO_PATHCONV=1 schtasks /Delete /TN Foo /F` is not flagged by core approval (False); that is the C39 hole.

Fix: 

Evidence: [repro] SKILL.md:42-49 now says to use `MSYS_NO_PATHCONV=1 <cmd> /switch` with one slash and to never double the slash. Repro review3-scratch/docs-fixcheck/v23_skill_forms.py (hvenv + core): detect_dangerous_command is True for 'MSYS_NO_PATHCONV=1 taskkill /F /PID 1234', for '... cmd /c rd /s /q', for icacls /grant and for bcdedit /set. It is still False for 'taskkill //F //PID 1234', 'cmd //c rd //s //q' and 'bcdedit //set'. The skill's examples work through executor.run: tasklist /FO CSV and ipconfig /all both rc 0. But README.md:116 still says native commands 'need the slash doubled, as in `cmd //c dir` or `tasklist //FO CSV`... The bundled hands skill tells the model this'. That is the opposite of SKILL.md:45-46, and the doubled form still runs (`cmd //c echo` gives rc 0 with its output).
[trace] SKILL.md:42-49 now leads with `MSYS_NO_PATHCONV=1 <cmd> /switch` and says 'Never double the slash ... hides the command from Hermes's approval check'. Core's approval check confirms that this form is the safe one. My review3-scratch/docs-trace/approval_forms.py run against core detect_dangerous_command returned True for `MSYS_NO_PATHCONV=1 taskkill /F`, `cmd /c rd /s /q`, `cmd /c del`, `icacls /grant`, `cipher /w` and `bcdedit /set`, and False for `taskkill //F //PID`, `cmd //c rd //s //q`, `cmd //c dir` and `tasklist //FO CSV`. But README.md:116 still says native commands 'need the slash doubled, as in `cmd //c dir` or `tasklist //FO CSV` ... The bundled hands skill tells the model this'. That sentence is false at HEAD and keeps the bypass spelling in the user-facing doc. The device-side fold of `//` to `/` was also not added: policy.py:54-58 normalizes only quotes and backslashes, and only the HermesHands-specific pattern at policy.py:15 accepts `/{1,2}`.

## R40 [major] (unfixed, votes 3/3) V27: V27: repro=partial, trace=partial
File: docs-ci-catalog:

[repro:partial] residual: Make the two pair printers agree. Make doctor require basic_auth, or reword its fix text.
[trace:partial] residual: Drop the undeclared public_url read at commands.py:118, or declare it and use it in cli.py too. Make doctor require basic_auth when public_url is a Hands-only host, or reword its message.

Fix: 

Evidence: [repro] README.md:44 and the catalog draft's known_issues (line 36) now state truthfully that only dashboard.basic_auth works with a Hands-only public host. The other residuals remain. commands.py:116-120 (/hands pair) prefers ctx.get_config('public_url'), a setting plugin.yaml config_schema does not declare, before dashboard.public_url, while cli.py:52-55 (hermes hands pair) reads only dashboard.public_url. So the two printers can still print different hosts. Doctor cli.py:106 passes 'Interactive dashboard auth' on any interactive provider, and its fix text says 'or another interactive auth provider', which contradicts README:44.
[trace] The docs are now truthful. README.md:44 says 'Only dashboard.basic_auth works in this setup: redirect sign-in providers ... /auth/callback, which the Hands host does not serve', and yaml:36 says the same. Two parts of the finding remain. (1) The pair printers still disagree. commands.py:117-121 prefers `ctx.get_config("public_url")`, which plugin.yaml:18-24 never declares, while cli.py:52-55 uses only dashboard.public_url. (2) Doctor still passes 'Interactive dashboard auth' for any interactive provider, OAuth included (cli.py:107 list_interactive_providers), and its fix text says 'or another interactive auth provider', which contradicts README:44.

## R59 [minor] (new_defect, votes 3/3) claims-and-admission:doctor-auth-check-and-public-url: V27 is only partly fixed: doctor passes and recommends sign-in providers that the README says cannot work, and /hands pair reads an undeclared public_url setting
File: hermes-hands/hands/cli.py:107

README Setup step 5 says only dashboard.basic_auth works, because OAuth/OIDC providers redirect to dashboard.public_url + /auth/callback, which the Hands-only nginx host does not serve. `hermes hands doctor` still passes 'Interactive dashboard auth' whenever any interactive provider is registered, and its fix text recommends 'basic_auth or another interactive auth provider'. An owner using OIDC gets all PASS and then a sign-in loop that ends in the nginx 404. Separately, commands.py:118-119 prefers ctx.get_config("public_url") over dashboard.public_url, but public_url is not in plugin.yaml's config_schema, while cli.py:53 reads only dashboard.public_url. The two pair printers can therefore print different URLs.

Fix: Have doctor check for the basic_auth provider specifically and name only basic_auth in its fix text. Drop the undeclared public_url read, or declare it in config_schema and use it in both printers.

Evidence: cli.py:107: _check("Interactive dashboard auth", bool(list_interactive_providers()), "Configure dashboard.basic_auth or another interactive auth provider."). README Setup step 5: 'Only dashboard.basic_auth works in this setup ...'. commands.py:118-119: ctx.get_config("public_url", default="") or dashboard.get("public_url"). plugin.yaml config_schema has no public_url key.

## R60 [minor] (new_defect, votes 3/3) claims-and-admission:readme-two-agents-claim-false: README says two agents for one device fail every command, but HEAD's own e2e test shows the commands succeed
File: README.md:110

The round 3 README says 'Run exactly one `hermes-hands serve` per device. Two agents for the same device fail every command.' The round 3 relay change (_note_instance, relay.py:386-408; _agent_gone_locked, relay.py:313-322) leaves jobs alone while both agents stay in touch, so commands succeed. The claim is false, and it hides the real effect: either agent may take a job, so a screenshot request can reach the agent that was not started with --allow-screen.

Fix: Say that a second agent for the same device takes some of its jobs, so a screenshot or a command may run in either process, and keep the advice to run exactly one.

Evidence: `pytest tests/test_e2e_device_relay.py -k two_agents` (hvenv, PYTHONPATH=hermes-v0.23.0) gives '1 passed'. The test asserts that 'echo both' returns 0 three times while a second Agent is running (test_e2e_device_relay.py:452-467).

## R61 [minor] (new_defect, votes 3/3) claims-and-admission:stale-line-citations: File:line citations in the PR surfaces table and the hook issue are stale at HEAD
File: C:\Users\gudma\AppData\Local\Temp\claude\243a2583c7948a43\3a0b990f-4523-40f0-902a-56d3b1f03ba8\scratchpad\hands-plan\submission\PR-DESCRIPTION.md:Hermes surfaces table; Validator warnings section; HOOK-REQUEST-ISSUE.md asks 5, 6, 7

Reviewers are told to read the pinned tree at these lines, but most of them now point at unrelated code. The 'Private' rows matter most, because a reviewer who checks them lands on the wrong code.

Fix: Regenerate every file:line citation from the final pinned SHA just before opening the PR.

Evidence: At HEAD: commands.py:130 (register_command) is now at :148; commands.py:124 (ask_choice) is at :142; cli.py:115 (register_cli_command) is at :117; cli.py:75 (set_config) is at :80; environment.py:278 (current_cron_execution) is at :304; environment.py:444-445 (private imports) is at :456-457; environment.py:288-301 (file ops cache) is at :314-327. In the hook issue, ask 5 cites environment.py:322-330 for env={'HANDS_RELAY_SEED': ''}, which is at :350-358, and asks 6 and 7 repeat the 444-445 and 288-301 citations. Checked with sed and grep -n on hermes-hands/hands/{commands,cli,environment}.py and against the validator output (environment.py:456/457).

## R62 [minor] (new_defect, votes 3/3) claims-and-admission:stale-test-counts: README and PR test counts are stale: they claim 180 tests, and 307 are collected
File: README.md:170

README:170 and the PR's 'Validation evidence' say '180 tests ... 126 plugin tests, 6 of them end to end ... and 54 device tests' and 'On Windows 178 pass and 2 are skipped'. HEAD has many more tests, so the pass and skip figures were not rerun either. V1 asked for real numbers.

Fix: Rerun both suites at the pinned SHA and update README:170 and the PR draft with the actual collected, passed and skipped counts.

Evidence: pytest --collect-only -q at HEAD gives: hermes-hands/tests -> '199 tests collected'; tests/test_e2e_device_relay.py -> '12 tests collected'; device/tests (shared .venv) -> '108 tests collected'. grep -c 'def test_' gives 195 plugin functions, 104 device functions and 12 in the e2e file. `git log -S"180 tests"` shows the line was last set in 2a74f1e, before rounds 2 and 3.

## R76 [minor] (new_defect, votes 3/3) gateway-security:v46-http-public-url: V46 still open: doctor PASSes an http public_url, and both pair printers print a command the device refuses
File: hermes-hands/hands/cli.py:106/227 (cli.py:51-58, commands.py:116-123)

Doctor checks dashboard.public_url only for being non-empty. hermes hands pair and /hands pair print public_url verbatim, or fall back to the loopback dashboard_url, which points at the device itself when the device is another machine. The device refuses any non-loopback http URL (device cli.py:30-37). An owner with http://hands.example.com sees all PASS and then gets a pairing command that cannot work. These lines are unchanged between db1a036 and 1586b18.

Fix: Make doctor FAIL a public_url that is neither https nor loopback, and have both printers warn or refuse in that case.

Evidence: Repro review3-scratch/gateway-security/gw_doctor_http.py (config dashboard.public_url http://hands.example.com): 'PASS dashboard.public_url', 'Run on the device: hermes-hands pair http://hands.example.com --name laptop --code=-Q3xk_Abc', then the real device _pair prints 'Pairing requires an https URL unless the gateway host is loopback' and 'device _pair rc: 1'. Code: cli.py `_check("dashboard.public_url", bool(public_url), ...)`; commands.py:117-122 gateway_url fallback chain printed unchanged.

## R77 [minor] (new_defect, votes 3/3) gateway-security:v47-non-loopback-dashboard-url: V47 still open: a non-loopback plain-HTTP dashboard_url is accepted, so the local bearer that authorises /local/exec goes over the LAN in cleartext while doctor shows all PASS
File: hermes-hands/hands/client.py:36-48 (cli.py:85-112, 206-233; plugin.yaml:21)

Round 3 added trust_env=False with the comment 'loopback only', but nothing enforces loopback. client.configure accepts any URL, and doctor only checks that the relay answers. plugin.yaml:21 still calls dashboard_url the 'Loopback address of the dashboard'. With a LAN http URL, every gateway call carries Authorization: Bearer <local bearer> plus the job scripts and stdin in clear. Anyone on the path who captures that bearer can queue arbitrary scripts on every paired device through /local/exec. V48 (proxy) is confirmed fixed.

Fix: In client.configure, refuse (configure nothing and report it) a dashboard_url whose scheme is http and whose host is not localhost or a loopback IP, as 862b763 did, and restore a doctor FAIL line for that case.

Evidence: Repro review3-scratch/gateway-security/doctor_lan.py (real client.configure, dashboard_url=http://192.168.1.20:9119, mock transport, real HERMES_HOME): all five doctor checks PASS, then "requests doctor sent: [('http', '192.168.1.20', '/api/plugins/hermes-hands/local/state', 'Bearer UBEaVI2SQ...')]". Repro review3-scratch/gateway-security/lan_dashboard_url.py with HTTP_PROXY=http://127.0.0.1:1: 'configure accepted non-loopback http dashboard_url: http://192.168.1.149:50916', 'state(): {'relay': True, ...}', 'server saw: 192.168.1.149 /api/plugins/hermes-hands/local/state bearer matches local bearer: True'.

## R78 [minor] (new_defect, votes 3/3) judge-and-gate:doctor-dashboard-url-loopback: V47 still open: doctor never checks that dashboard_url is loopback, so a plain-HTTP LAN dashboard_url carrying the local bearer passes
File: hermes-hands/hands/cli.py:85

Doctor (cli.py:85-112) checks the backend, that the relay answers, that public_url is set, interactive auth and the shared store. It never checks that `dashboard_url` is loopback. The gateway sends the local bearer and every job script to dashboard_url (__init__.py:285-288). An http LAN or VPS address passes doctor, and the bearer then travels in clear text.

Fix: Add a doctor check that fails when the host of dashboard_url is not loopback.

Evidence: `grep -n "dashboard_url\|loopback" hermes-hands/hands/cli.py` returns only :54 (the pair fallback) and :105 (the 'Relay answers at dashboard_url' label), with no loopback check. V47 is in ROUND2-VERIFY.md:419.

## R79 [minor] (new_defect, votes 3/3) judge-and-gate:doctor-public-url-http: V46 still open: doctor passes an http public_url, both pair printers print a URL the device refuses, and the Quick start implies a same-LAN device needs no public route
File: hermes-hands/hands/cli.py:106

The device refuses any non-loopback http pairing URL (device cli.py:36-38). The gateway's doctor passes any non-empty `dashboard.public_url` (`bool(public_url)`, cli.py:106). `hermes hands pair` (cli.py:52-56) and `/hands pair` (commands.py:116-123) print the URL as given, or fall back to the loopback dashboard_url, which on the laptop points at the laptop itself. README:12 says only that a device on another network needs the public route. So a judge can pass every doctor check and then hit 'Pairing requires an https URL...' or connection refused on the laptop. No round 3 lane was assigned V46.

Fix: Make doctor fail a public_url that is neither https nor loopback, have both printers warn when they print a non-https URL, and reword README:12 to 'any device not on the gateway host needs the HTTPS public route'.

Evidence: cli.py:106 `_check("dashboard.public_url", bool(public_url), ...)`. cli.py:52-55 and commands.py:116-121 print without validating. README:12 reads 'A device on another network also needs the public route in [Setup](#gateway).' The earlier repro is gw_doctor_http.py (ROUND2-VERIFY.md:410-417), and the code at those lines has not changed.

## R83 [minor] (new_defect, votes 3/3) windows:readme-doubled-slash: README still teaches the doubled-slash form (`cmd //c`, `tasklist //FO`), which evades Hermes approval, and wrongly says the skill teaches it
File: README.md:116

The V23 fix landed only in SKILL.md:42-49, which says never double the slash and to use MSYS_NO_PATHCONV=1. README.md:116 still says native commands 'need the slash doubled, as in `cmd //c dir` or `tasklist //FO CSV` ... The bundled `hands` skill tells the model this.' Both halves are false. Core's Windows approval patterns need whitespace directly before a single slash (approval_detection.py:467 `\btaskkill\b[^\n]*\s/f\b`, also :476-477), so `taskkill //F` gets no approval prompt.

Fix: Replace README.md:116 with the SKILL's MSYS_NO_PATHCONV=1 rule and remove the doubled-slash examples.

Evidence: Compare README.md:116 with hermes-hands/skills/hands/SKILL.md:42-49, against hermes-v0.23.0/tools/approval_detection.py:467, 476-477. pc.sh in Git Bash: `MSYS_NO_PATHCONV=1 cmd /c echo hi-from-cmd` printed hi-from-cmd with rc 0, and `MSYS_NO_PATHCONV=1 tasklist /FO CSV /NH /FI "IMAGENAME eq lsass.exe"` printed the CSV row.

## Critic

- [minor] Unverdicted round-2 id V35 (device transient-status warning): Neither checker gave V35 a verdict, and it is still open at HEAD. last_auth_warning still starts at 0.0 (loop.py:49). On every transient 401/409/429, _verify_response raises ValueError (loop.py:82-87), and serve's catch-all then logs 'Device poll failed; retrying' on every attempt (loop.py:344-346). So the 'one warning per minute' rule in W4 is not met. The throttled text (loop.py:120) also leaves out the likely causes W4 requires: clock skew over 300 s, a dashboard started without the seed, and a device revoked before this version. No repo test covers the text or the throttle.
  Evidence: Repro C:\Users\gudma\AppData\Local\Temp\claude\243a2583c7948a43\3a0b990f-4523-40f0-902a-56d3b1f03ba8\scratchpad\review3-scratch\critic\v35_repro.py (run from device/ with PYTHONPATH=. and HOME/USERPROFILE set to the scratch dir, shared venv). A MockTransport answers every poll with a bare 401. Output: 'last_auth_warning initial: 0.0', 'polls: 5', one 'Relay returned HTTP 401; retrying. Confirm this device is still paired...' line, then 'Device poll failed; retrying' five times, once per poll. The retry cap of 3 s (loop.py:125-130, cap=3) means about 20 or more generic warnings a minute.
- [minor] Unverdicted round-1 ids C13, C34, C40, C49 and contract items W1, W4: Neither checker returned a verdict for these. I traced each one at HEAD. C13 and C40 are fixed by trace: doctor now calls client.state() and no longer GETs the dashboard root (hermes-hands/hands/cli.py:96-115), and status pings /local/state before it reports the relay down (cli.py:27-35, commands.py:74-82). The C40 path comparison of plugin_data_dir is still not implemented. The 'Shared plugin_data_dir' check is still only relay_alive() (cli.py:111-115), against PLAN-v3 §1.1. C34 is fixed by trace: the device catches httpx.HTTPError and gives a 401-specific hint (device cli.py:43-51), and both printers refuse without a seed (commands.py:111-112, cli.py:45-47). C49 is fixed (--code= form at commands.py:122 and cli.py:56), but no regression test exists: grep for 'code=-' in both test trees finds nothing. W1 and W4 are covered only indirectly, through C15, C12, C33 and C47.
  Evidence: argparse check with the shared venv: shlex.split('hermes-hands pair https://h --name laptop --code=-AbC_x') parses to Namespace(code='-AbC_x'). grep -rn "code=-" device/tests hermes-hands/tests returned no hits.
- [minor] Device name 'server' shadowed by the gateway target (not reviewed by any lane): Device names are never checked against the reserved target name 'server'. /hands pair takes any name (commands.py:107-122), mint_pairing_code stores it as given (store.py:112-120), and relay.pair accepts it (relay.py:438-462). But targets.SERVER == 'server' (targets.py:5), and every routing check uses name equality: commands.py:125/128/135 and environment.py:342/442/570. So a device paired as 'server' (README:7 suggests 'a GPU box, or a Pi', a plausible home server) can never be targeted. `/hands use server` then silently sends the owner's commands to the gateway host instead of the device the owner meant. /hands revoke server and accept-policy server do act on the device row, which adds to the confusion.
  Evidence: Code path only, no live repro: commands.py:107-122 has no name check, relay.py:430-470 has no reserved-name refusal, and targets.py:5 plus environment.py:342 send 'server' to the gateway's LocalEnvironment.
- [minor] Surfaces that no round-3 lane exercised: hermes-hands/desktop/plugin.js and dashboard/dist/index.js have not changed since 862b763 (git diff --stat 862b763 HEAD shows only dashboard/plugin_api.py). Round 3 changed the replies they parse: messages.py gained 21 lines, /hands status now adds cause lines after the status line (commands.py:85-86), and pair now refuses in shared chats. No lane re-ran the Desktop pane's regexes (plugin.js:36-39) against the new replies. The pane treats every line after the status line as an alert, so relay error text now shows as device alerts. POSIX device paths were also only reasoned about: posix-reap-kills-leader-only comes from code reading, and no lane reports running the device suite or the e2e on Linux or WSL.
  Evidence: git diff --stat 862b763 HEAD -- hermes-hands/desktop hermes-hands/dashboard printed '1 file changed' (plugin_api.py only). The PAIR_CODE, PAIR_COMMAND and STATUS_TARGET regexes are at plugin.js:36-39.
- [minor] Weak 'fixed' verdict on C40 / doctor claims vs README: README:76 says doctor checks 'that the gateway and the dashboard use the same HERMES_HOME'. The code only checks that a relay heartbeat row is visible after client.state() (cli.py:111-115). That proves a shared store only indirectly and never compares paths. C40 was marked closed by both checkers, but the PLAN-v3 path comparison it asked for was never built.
  Evidence: hermes-hands/hands/cli.py:111-115 runs _check('Shared plugin_data_dir', store.relay_alive(), ...), and no code reads the dashboard's plugin_data_dir.
