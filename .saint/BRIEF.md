# Saint build brief: Hermes Hands round 4, docs, CI and doctor lane

Hi Saint. You're one of three builders this round. Grok has the gateway code and Codex has the device code. Your lane is the docs, CI and doctor checks. Opus audits every lane before anything merges.

**Repo:** github.com/Ridwannurudeen/hermes-hands (private; you're a collaborator). It's a Hermes Agent v0.23.0 terminal-backend plugin that runs Hermes's terminal and file tools on the user's own paired laptop. The gateway half is in `hermes-hands/` and the device agent is in `device/`.

**Your branch:** `saint/round4-docs`, cut from main at 1586b18. Push your commits there. Don't push to main and don't open a PR; the orchestrator merges after the audit.

**The findings:** `.saint/ROUND4-ITEMS.md` on your branch. Each item was confirmed 2 of 3 by adversarial reviewers, with evidence. Fix every item in it.

**Your files** (touch nothing else, since others are editing the rest right now):
- README.md
- hermes-hands/skills/hands/SKILL.md
- hermes-hands/plugin.yaml (text only)
- hermes-hands/hands/cli.py (the doctor and pair printer code only)
- hermes-hands/desktop/plugin.js and hermes-hands/dashboard/dist/index.js
- .github/workflows/* and scripts/
- their tests: hermes-hands/tests/test_cli.py and test_desktop_and_skill.py

**The work:**
1. **R5, R16, R28, R39, R83:** take every doubled-slash piece of advice (`cmd //c`, `tasklist //FO`) out of the README. It skips Hermes's approval check. Make the README match SKILL.md, which says to run native Windows programs as `MSYS_NO_PATHCONV=1 <program> /switch` and never to double the slash.
2. **R17, R26, R30, R62:** test counts. Replace every test count in the README with the placeholder `[COUNT_AT_PIN]`. The orchestrator fills them in after the final run.
3. **R60:** README says two agents for one device fail every command. That's false. Device builds now refuse a second `serve` on the same device home, so state that instead.
4. **R59, R40, R76 to R79, doctor (cli.py):**
   - doctor FAILs an `http://` public_url and a non-loopback dashboard_url;
   - with a Hands-only public host it requires `dashboard.basic_auth`, or its fix text says truthfully what works;
   - the two pair printers print the same URL;
   - drop the read of the undeclared `public_url` plugin setting.
5. **Critic:** "doctor checks the same HERMES_HOME" must say exactly what doctor checks: that the relay heartbeat row is visible after `client.state()`. Also check that the Desktop pane still parses the current `/hands` status output.
6. **R2:** state both install routes truthfully in the README, including that the PyPI package and the public repo are not published yet.
7. **R6, R37, R61:** these items are about the PR draft and the hook-request issue, which aren't in the repo. Don't edit anything for them. Just list in your report every private core import you find in `hermes-hands/` at your branch tip, with file:line.

**Rules:**
- Every claim in the docs must be true of the code. Check it in the code before you write it.
- User-facing text is plain English with no em or en dashes.
- Don't add comments to code you didn't change, and leave no dead code.
- Run the tests in your lane (`python -m pytest hermes-hands/tests/test_cli.py hermes-hands/tests/test_desktop_and_skill.py -q` from the repo root, with Hermes v0.23.0 importable if you have it) and say what you ran.
- Your last commit deletes the `.saint/` folder.

**When done:** reply with your branch tip SHA and a short report. For each R number, say whether it's fixed or skipped and why, and which files changed.
