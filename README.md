# Hermes Hands

*Your own laptop as Hermes's hands. Native Windows. Nothing installed there but Python.*

Hermes keeps running on your server. Hermes Hands pairs a laptop with a one-time code and lets Hermes run terminal and file tools on that device. The device connects out over HTTPS and does not listen for inbound connections.

The gateway and device sign messages with Ed25519 keys pinned during pairing. Hermes approvals run before a command reaches the device. If a device is offline, revoked, or the relay is unreachable, the device command fails and is not sent to another target.

## How it compares

| | dirtside | hermes-teleoperator | Hermes Hands |
|---|---|---|---|
| Device systems | macOS and Linux; its reported internet run used a WSL2 device | Linux receiver | Native Windows with Git Bash and Linux; macOS code has unit tests but no live device test |
| Device install | Hermes on both ends | Rust receiver over SSH | `pipx install hermes-hands==0.1.0` |
| Connection | Outbound long poll | Inbound SSH | Outbound HTTPS long poll |
| Signing | Ed25519, pinned keys | SSH | Ed25519 in both directions, pinned keys, no shared secret |
| Shell and file tools | Yes | Yes | Yes |
| Screenshot from chat | Can be assembled ad hoc on macOS or Linux | No | First class screenshot tool, requires consent |
| Relay unavailable | Fails closed | Not applicable | Device target fails closed; `server` does not need the relay |
| Evidence | 619 tests and 8/8 internet run, as reported by its project | 69 tests, as reported by its project | Contract, attack and end to end tests, CI workflows for Ubuntu and Windows; live checks described below |

Hermes Hands is an independent implementation with no shared code with those projects. Remote backends such as `coder`, `cloudflare-sandbox`, and `cua-spaces` also exist; a Cua Space is a separate sandbox, not your real desktop.

## Quick start

### Gateway

1. Install Hermes v0.23.0 or later and enable the plugin from the Hermes plugin catalog. The plugin requires Hermes `>=0.23.0`.
2. Run `hermes hands setup` on the gateway host. It creates the relay seed if missing and asks for the owner Telegram user ID. Configure Telegram and its owner access in Hermes.
3. Restart the gateway and dashboard if setup created the seed.
4. Set the dashboard URL and authentication in Hermes configuration. The dashboard should listen on loopback. Set `dashboard.public_url` to its public HTTPS URL and configure `dashboard.basic_auth` or another interactive dashboard authentication provider.
5. Start the dashboard on loopback, for example: `hermes dashboard --host 127.0.0.1 --port 9119 --no-open`.
6. Run the placement line that setup prints: `hermes config set bot_desktop.placement gateway`. Without it, Hermes refuses to start the browser and computer use tools under any plugin terminal backend, for every conversation in the profile.
7. Pair a named device. In Telegram send `/hands pair laptop`, or on the gateway host run `hermes hands pair laptop`. Either prints a one-time code and the exact device command. Pairing from chat is owner only.
8. Select the paired device with `/hands use laptop`. `/hands use server` switches terminal calls back to the gateway host.

For a public dashboard behind nginx, expose the device pair and poll routes only. Example locations inside the HTTPS server block:

```nginx
location ~ ^/api/plugins/hermes-hands/(device/|pair$) {
    proxy_pass http://127.0.0.1:9119;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Real-IP $remote_addr;
}

location / {
    return 404;
}
```

Keep the dashboard bound to loopback. Do not expose `/local/*` or `/ui/*` through this public route.

### Windows device

Open PowerShell and install Python and Git for Windows:

```powershell
winget install Python.Python.3.12
winget install Git.Git
```

Open a new terminal so the updated PATH is loaded, then install pipx and ensure its scripts directory is on PATH:

```powershell
py -m pip install --user pipx
py -m pipx ensurepath
```

Open a new terminal again, then install the pinned device agent:

```powershell
pipx install hermes-hands==0.1.0
```

Use the pairing command printed by `/hands pair laptop`, substituting the displayed URL, name, and code:

```powershell
hermes-hands pair https://hands.example.com --name laptop --code ONE_TIME_CODE
hermes-hands serve
```

Screen capture is off by default. To opt in for a running device, use `hermes-hands serve --allow-screen`. This option is present in the CLI; screenshot delivery has not yet been verified live.

To install the Windows scheduled task service, run `hermes-hands install-service`. `hermes-hands install-service --dry-run` prints the planned action and `hermes-hands install-service --remove` removes the task.

Optional: `winget install BurntSushi.ripgrep.MSVC` enables ripgrep features in search. `hermes-hands doctor` reports whether Git Bash and Python are available inside it. The device needs Python on Git Bash's PATH for some file operations.

### Linux device

Install Python 3.11 or later, pipx, and a Bash environment, then install `hermes-hands==0.1.0` with pipx. Pair using the command printed by `/hands pair <name>`, then run `hermes-hands serve`. The `install-service` command is implemented for Windows Task Scheduler.

## Where commands run

| Situation | Target and behavior |
|---|---|
| New conversation | `default_target`, which defaults to `server` |
| `/hands use <name>` | That paired device on the next command in this conversation |
| `/hands use server` | Local terminal environment on the gateway host |
| Device offline | Fails with an offline message; nothing runs elsewhere |
| Device revoked or unknown | Fails closed; nothing runs elsewhere |
| Relay down or unreachable | Device calls fail closed; `server` calls still run locally |
| Cron execution | Always `server` |
| Delegated cron child | Uses the configured default because the child has no cron execution context |

The target is selected for each terminal call. The first terminal result after a target change identifies the target and device home. The server target uses Hermes's local environment, with normal differences from the device target.

## Security model

Pairing and target switching from chat are owner gated. Pairing codes are short lived and single use. The device keeps its private signing key locally; the gateway stores device public keys. Requests have freshness and replay checks, and the relay signs work before the device executes it. The device connects out and does not open a listening port.

Hermes's existing approval gate runs first. A device policy deny list adds a second guard. The relay checks the device policy hash against the hash recorded at pairing; if it changes, the relay refuses work until the owner accepts the changed policy. The list is a guard rail, not a sandbox.

The agent runs as the logged in device user. A same-user shell can read or change any file that user can access, including credentials. The deny list cannot isolate the agent from that user's permissions.

| Threat | Control |
|---|---|
| An agent prompt tries to pair a device | Pairing is an owner initiated command, not a model tool |
| Pairing code is exposed | One use, ten minute expiry, owner minted |
| Device key is stolen | Revoke the device at the gateway; private key stays on the device |
| Forged or replayed request | Ed25519 signatures, freshness window, nonce cache, protocol version |
| Public dashboard exposure | Loopback bind, HTTPS public URL, interactive dashboard authentication; nginx exposes pair and device routes only |
| Destructive device command | Hermes approval first, device deny list second |
| Agent alters policy or unpairs itself | Policy hash check and local deny patterns; owner can revoke from gateway |
| Unattended cron job | Runs on `server`; no person is available to approve a device action |
| Screenshot data | Screen capture is opt in; image is sent to the configured model provider when vision analysis is requested |

### What is stored where

The gateway stores the relay seed through Hermes's declared secret configuration and stores device public keys and pairing metadata in the plugin database. It does not store device private keys. The device stores its Ed25519 private key in the OS keyring when the `keyring` extra is installed; otherwise it uses `~/.hermes-hands/device.json`. On Windows, the fallback file is restricted with an owner only ACL. Device output logs are stored under `~/.hermes-hands/out/`.

### Why this is a plugin and device agent, not a skill

A skill gives Hermes instructions. Hands also needs a gateway terminal backend, authenticated relay routes, device pairing and a process running on the laptop to receive signed work. The skill alone cannot provide those pieces.

## Disclosures

The plugin catalog description says:

> Your own laptop as Hermes's hands. Hermes stays on your server; the terminal and file tools, and a screenshot you consented to, run on a laptop you paired (native Windows with Git Bash, or Linux; no Hermes on the laptop). The laptop dials out over HTTPS and never listens; both ends sign with Ed25519 keys pinned at pairing; Hermes approvals stay on; an offline, revoked or unreachable device fails closed. Select it with terminal.backend: hands.

The device package installs dependencies that include `cryptography`, `mss`, Pillow and `psutil`; these may install compiled wheels. The optional `keyring` extra uses the platform keyring. The gateway plugin declares no Python dependencies. No binaries are committed in this repository.

Two Hermes validator warnings are disclosed: the plugin imports the `hermes_cli.dashboard_auth.token_auth` submodule and `tools.environments.local.LocalEnvironment`. On Hermes v0.23.0, `hermes plugins validate --install-deps` passed with exactly those two warnings.

## Desktop pane

In Hermes Desktop the plugin adds a **Hands** pane on the right and a status bar chip.

1. The pane shows whether the relay is up, a picker for where the current conversation runs (the server or a paired device), every paired device with its OS, an online dot, last seen time and policy state, and any policy alerts.
2. The Pair form mints a code and shows the exact device command with a copy button.
3. The chip reads `hands: <target>` and adds `| relay down` when the relay is unreachable. Clicking it opens the pane.

The pane reads `/ui/state` every two seconds while it is visible and every ten seconds for the chip, and refreshes early on a device change event. Switching targets and pairing go through the same owner checked `/hands` commands as chat. The file uses only the Desktop plugin SDK and passes Hermes's desktop surface check. The pane has not yet been exercised inside the running Desktop app.

## Windows notes

Windows commands run through Git Bash. The device process itself is Python and does not run Hermes or WSL. Ensure `python` or `python3` resolves inside Git Bash, and install ripgrep if you need its additional search behavior. The policy deny list is not a security boundary against another process running as the same user. Timeouts and cancellation terminate the process tree with Windows process tools.

Without ripgrep, `search_files` on a device uses find and grep. Hermes only refuses broad searches on its own host filesystem, so on a device a search rooted at the home directory or a drive root runs, slowly. Search the narrowest folder you can. Modified time ordering needs ripgrep 14 or GNU find support. Device output is capped at 64 MiB; at most 1 MiB is returned inline for terminal output, with the full log kept on the device.

## Limits and known issues

- macOS device code has unit test coverage in CI, but no macOS device was tested live for this release.
- Telegram live testing has not been completed.
- The Desktop pane is pending.
- Screenshot capture and delivery have not been tested live.
- Streaming tool output has not been verified; results arrive when a tool call ends.
- The device target requires the relay and a running device agent. It does not silently fall back to the gateway.
- On macOS, screen capture requires Screen Recording permission. Linux screen capture supports X11.
- `install-service` is for Windows Task Scheduler.

## Beyond one laptop

The same pairing model can be used for another workstation, a GPU box, or a Pi.

## Live checks

On a real Hermes v0.23.0 gateway in WSL driving a native Windows 11 laptop, pairing succeeded; terminal work ran on the laptop with a target note; the working directory persisted across commands; and a 200,001 byte `write_file` round trip matched the laptop disk by SHA-256. A timeout returned 124 in 3.3 seconds. When the laptop was offline, the command returned 255 in 0.1 seconds and nothing ran. After the agent restarted, it recovered, and the offline command was not replayed.

These checks do not cover Telegram, the Desktop pane, screenshots, or macOS.

## Tests and validation

From the repository root, run the device and plugin suites with:

```powershell
.venv\Scripts\python -m pytest device/tests hermes-hands/tests -q
```

The device CI tests on Ubuntu, Windows, and macOS with Python 3.11 and 3.12. Plugin CI tests on Ubuntu and Windows against Hermes v0.23.0. CI also validates the plugin, checks the catalog shape and payload, and scans Python changes for Windows footguns.

## Install from Hermes

[Install Hermes Hands](hermes://plugin/install?repo=<owner>/hermes-hands&enable=1)

## License

MIT
