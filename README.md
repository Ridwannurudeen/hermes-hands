# Hermes Hands

Ask the Hermes on your server what is on your laptop, and it runs the command or reads the file on the laptop itself.

Hermes Hands pairs a laptop with a one-time code. The device connects out over HTTPS and does not listen for inbound connections. Hermes approvals run before a command reaches the device. If a device is offline, revoked, or the relay is unreachable, the device command fails and is not sent to another target.

**Beyond one laptop:** use the same pairing model for another workstation, a GPU box, or a Pi.

## Quick start

1. On the gateway, install the plugin with `hermes plugins install hermes-hands`, then run `hermes hands setup` and follow its prompts.
2. Pair a named device with `/hands pair laptop` in Telegram or `hermes hands pair laptop` on the gateway, then run the printed `hermes-hands pair` command on the device.
3. In the conversation, run `/hands use laptop`.

## Versions

Hermes Hands 0.1.0 for the gateway plugin and device package, same version. Requires Hermes 0.23.0 or later.

## How it compares

| Capability | dirtside | hermes-teleoperator | Hermes Hands |
|---|---|---|---|
| Device systems | macOS and Linux; its reported internet run used a WSL2 device | Linux receiver | Native Windows with Git Bash and Linux; macOS code has unit tests but no live device test |
| Device install | Hermes on both ends | Rust receiver over SSH | `pipx install hermes-hands==0.1.0` |
| Connection | Outbound long poll | Inbound SSH | Outbound HTTPS long poll |
| Signing | Ed25519, pinned keys | SSH | Ed25519 in both directions, pinned keys, no shared secret |
| Shell and file tools | Yes | Yes | Yes |
| Screenshot from chat | Not a built in tool | No | Built in screenshot tool, requires consent on the device |
| Relay unavailable | Fails closed | Not applicable | Device target fails closed; `server` does not need the relay |

Hermes Hands is an independent implementation with no shared code with those projects. Remote backends such as `coder`, `cloudflare-sandbox`, and `cua-spaces` also exist; a Cua Space is a separate sandbox, not your real desktop.

## Setup

### Gateway

1. Install Hermes 0.23.0 or later and enable Hermes Hands from the plugin catalog or with `hermes plugins enable hermes-hands`.
2. Run `hermes hands setup` on the gateway host. It creates the relay seed if missing and asks for the owner Telegram user ID. Configure Telegram and its owner access in Hermes.
3. Restart the gateway and dashboard if setup created the seed. Set the dashboard URL and authentication in Hermes configuration. The dashboard should listen on loopback. Set `dashboard.public_url` to its public HTTPS URL and configure `dashboard.basic_auth` or another interactive dashboard authentication provider.
4. Start the dashboard on loopback, for example: `hermes dashboard --host 127.0.0.1 --port 9119 --no-open`.
5. Set the placement line printed by setup: `hermes config set bot_desktop.placement gateway`. Without it, Hermes refuses to start browser and computer use tools under any plugin terminal backend, for every conversation in the profile.
6. For a public dashboard behind nginx, expose only the device pair and poll routes on a dedicated host name used only by Hands. Replace the example host and TLS certificate paths with your own existing certificate configuration.

```nginx
server {
    listen 443 ssl;
    server_name hands.example.com;
    ssl_certificate /path/to/existing/fullchain.pem;
    ssl_certificate_key /path/to/existing/privkey.pem;

    location ~ ^/api/plugins/hermes-hands/(device/|pair$) {
        proxy_pass http://127.0.0.1:9119;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Real-IP $remote_addr;
    }

    location / {
        return 404;
    }
}
```

Do not add the `return 404` location to a host that serves anything else. Keep the dashboard bound to loopback. Do not expose `/local/*` or `/ui/*` through the public route.

### Device

On Windows, in PowerShell:

1. Install Python and Git for Windows:

   ```powershell
   winget install Python.Python.3.12
   winget install Git.Git
   ```

2. Open a new terminal so the updated PATH loads, then install pipx:

   ```powershell
   py -m pip install --user pipx
   py -m pipx ensurepath
   ```

3. Open a new terminal again, then install the device agent:

   ```powershell
   pipx install hermes-hands==0.1.0
   ```

Pair using the command printed by `/hands pair laptop`, substituting the displayed URL, name, and code:

```powershell
hermes-hands pair https://hands.example.com --name laptop --code ONE_TIME_CODE
hermes-hands serve
```

Screen capture is off by default. To opt in for a running device, use `hermes-hands serve --allow-screen`. To install the Windows scheduled task service, run `hermes-hands install-service`. `hermes-hands install-service --dry-run` prints the planned action and `hermes-hands install-service --remove` removes the task.

Linux devices need Python 3.11 or later, pipx, and a Bash environment. Install `hermes-hands==0.1.0`, pair using the command printed by `/hands pair <name>`, then run `hermes-hands serve`. The service installer is implemented for Windows Task Scheduler.

Optional on Windows: `winget install BurntSushi.ripgrep.MSVC` enables ripgrep features in search. `hermes-hands doctor` reports whether Git Bash and Python are available inside it. The device needs Python on Git Bash's PATH for `execute_code` and some file operations.

## What runs on the device

With Hermes 0.23.0, Hands routes these tools through the selected device terminal backend: `terminal`, `execute_code`, `read_file`, `write_file`, `patch`, and `search_files`. `execute_code` needs Python available inside Git Bash. Hands also registers `hands_screenshot` and `hands_devices` as its own tools.

The target is selected for each terminal call. The first terminal result after a target change identifies the target and device home. `/hands use server` switches terminal calls back to the gateway host. New conversations use `default_target`, which defaults to `server`; cron execution always uses `server`. A device command never falls back to the gateway.

## Security model

The relay is trusted; whoever controls the gateway and dashboard can command every paired device, and the signatures protect the path between them, not against a compromised gateway. Pairing and target switching from chat are owner gated. The device keeps its private signing key locally; the gateway stores device public keys. Requests have a 300 second freshness window and the relay's nonce cache keeps used nonces for 600 seconds. Pairing codes expire after 600 seconds and are single use.

Hermes's existing approval gate runs first. A device policy deny list adds a second guard. The relay checks the device policy hash against the hash recorded at pairing; if it changes, the relay refuses work until the owner accepts the changed policy. The list is a guard rail, not a sandbox.

The agent runs as the logged in device user. A same-user shell can read or change any file that user can access, including credentials. The deny list cannot isolate the agent from that user's permissions.

The device connects out and does not open a listening port. Ed25519 signatures authenticate requests in both directions. They detect forged or replayed requests between the paired device and relay but do not protect against a compromised gateway.

## Stored data and retention

The gateway stores the relay seed through Hermes's declared secret configuration, and stores device public keys and pairing metadata in the plugin database. It does not store device private keys. The device stores its Ed25519 private key in the OS keyring when the `keyring` extra is installed; otherwise it uses `~/.hermes-hands/device.json`. The keyring service is `hermes-hands` and the entry username is `device-private`. On Windows, the fallback file is restricted with an owner only ACL.

Device output logs are written under `~/.hermes-hands/out/`. They are not deleted automatically. Device temporary shell and cwd files matched by the janitor are removed after 24 hours, with cleanup at startup and hourly. Delete output logs with `rm -rf ~/.hermes-hands/out` or remove selected files there.

Screenshots are written on the gateway under `$HERMES_HOME/cache/hermes-hands/` (or the configured Hermes home cache directory). They are not deleted automatically. Remove them with `rm` or delete the files in that directory.

## Latency

About 0.4 s per small command (p50 409 ms, p90 411 ms over 30 runs, measured twice) from a Hermes v0.23.0 gateway in WSL to a Windows 11 laptop on the same machine, network latency excluded. Most of what remains is Hermes's own per command shell wrapper and its exit polling.

## Why a plugin, not a skill

1. A terminal backend core routes terminal and file tools through the selected environment.
2. An authenticated relay routes work to paired devices and handles pairing.
3. A process on the laptop receives signed work and enforces its own device policy.
4. Per conversation target selection and the Desktop pane let users choose and inspect the active device.

A skill can explain how to use a feature, but cannot register a terminal backend, provide authenticated relay routes, run a laptop process, or enforce local policy.

## Tests and validation

180 tests run against Hermes 0.23.0: 126 plugin tests, 6 of them end to end with the real device agent and the real relay in one process, and 54 device tests. On Windows 178 pass and 2 are skipped by design (one needs a Linux gateway, one is POSIX only). CI workflows cover Ubuntu and Windows for the plugin, and Ubuntu, Windows, and macOS for the device.

The device CI tests on Python 3.11 and 3.12. Plugin CI validates the plugin and doctor, runs the plugin suite against Hermes 0.23.0, validates catalog shape, scans plugin payloads, and checks Python changes for Windows footguns.

## Not yet verified live

- Telegram interactions
- Screenshot capture and delivery
- The Desktop pane inside the app
- macOS devices

This list will shrink as live tests land. Live checks on a real Hermes 0.23.0 gateway in WSL and a native Windows 11 laptop confirmed pairing, terminal work on the laptop, persistent working directory, a 200,001 byte `write_file` round trip verified by SHA-256, timeout handling, offline failure without execution, and recovery after the device restarted.

## Uninstall

1. On the device, run `hermes-hands install-service --remove` if the Windows service is installed, then run `hermes-hands unpair`.
2. On the device, remove `~/.hermes-hands` and delete the keyring entry with service `hermes-hands` and username `device-private`. If `keyring` is unavailable, remove the fallback key file in `~/.hermes-hands`.
3. On the device, uninstall the package with `pipx uninstall hermes-hands`.
4. On the gateway, revoke the paired device with `hermes hands revoke laptop` (replace `laptop` with its name), then disable the plugin with `hermes plugins disable hermes-hands`.
5. On the gateway, set `terminal.backend` back to the backend you used before Hands.

## Install from Hermes

[Install Hermes Hands](hermes://plugin/install?repo=Ridwannurudeen/hermes-hands&enable=1)

Repository: [Ridwannurudeen/hermes-hands](https://github.com/Ridwannurudeen/hermes-hands)

## License

MIT
