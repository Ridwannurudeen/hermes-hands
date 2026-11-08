# Hermes Hands

Ask the Hermes on your server what is on your laptop, and it runs the command or reads the file on the laptop itself.

Hermes Hands pairs a laptop with a one-time code. The device connects out over HTTPS and does not listen for inbound connections. Hermes approvals run before a command reaches the device. If a device is offline, revoked, or the relay is unreachable, the device command fails and is not sent to another target.

**Beyond one laptop:** use the same pairing model for another workstation, a GPU box, or a Pi.

## Quick start

1. On the gateway host (Linux, or Windows inside WSL), install the plugin with `hermes plugins install Ridwannurudeen/hermes-hands/hermes-hands --enable`, then run `hermes hands setup` and follow its prompts. Once Hermes Hands is in the plugin catalog, `hermes plugins install hermes-hands` installs the reviewed version instead.
2. Run the two lines setup prints, `hermes config set terminal.backend hands` and `hermes config set bot_desktop.placement gateway`. Restart the gateway, and start the dashboard on loopback with `hermes dashboard --host 127.0.0.1 --port 9119 --no-open`. A device on another network also needs the public route in [Setup](#gateway).
3. Pair a named device with `/hands pair laptop` in a direct Telegram chat with the bot or `hermes hands pair laptop` on the gateway. On the device, run the printed `hermes-hands pair` command, then `hermes-hands serve`.
4. In a direct chat with the bot, run `/hands use laptop`.

## Versions

Hermes Hands 0.1.0 for the gateway plugin and device package, same version. Requires Hermes 0.23.0 or later.

## How it compares

| Capability | dirtside | hermes-teleoperator | Hermes Hands |
|---|---|---|---|
| Device systems | macOS and Linux; its reported internet run used a WSL2 device | Linux receiver | Native Windows with Git Bash, tested live; Linux and macOS code has unit tests but no live device test |
| Device install | Hermes on both ends | Rust receiver over SSH | `pipx install hermes-hands==0.1.0` |
| Connection | Outbound long poll | Inbound SSH | Outbound HTTPS long poll |
| Signing | Ed25519, pinned keys | SSH | Ed25519 in both directions, pinned keys, no shared secret |
| Shell and file tools | Yes | Yes | Yes |
| Screenshot from chat | Not a built in tool | No | Built in screenshot tool, requires consent on the device |
| Relay unavailable | Fails closed | Not applicable | Device target fails closed; `server` does not need the relay |

Hermes Hands is an independent implementation with no shared code with those projects. Remote backends such as `coder`, `cloudflare-sandbox`, and `cua-spaces` also exist; a Cua Space is a separate sandbox, not your real desktop.

## Setup

### Gateway

The gateway runs on Linux, or on Windows inside WSL. A native Windows gateway is not supported.

1. Install Hermes 0.23.0 or later, then install and enable Hermes Hands with `hermes plugins install Ridwannurudeen/hermes-hands/hermes-hands --enable`. After Hermes Hands is admitted to the plugin catalog, install it from the catalog or with `hermes plugins install hermes-hands`, which installs the reviewed commit.
2. Run `hermes hands setup` on the gateway host. It creates the relay seed if missing and asks for the owner Telegram user ID. Configure Telegram and its owner access in Hermes.
3. Select the Hands terminal backend: `hermes config set terminal.backend hands`. Without it Hermes keeps its default `local` backend, every command runs on the gateway host, and `/hands use <device>` refuses to switch.
4. Set the placement line printed by setup: `hermes config set bot_desktop.placement gateway`. Without it, Hermes refuses to start browser and computer use tools under any plugin terminal backend, for every conversation in the profile.
5. Set the dashboard URL and authentication in Hermes configuration. Set `dashboard.public_url` to the public HTTPS URL of the Hands host that nginx serves in step 7, for example `https://hands.example.com`, and configure `dashboard.basic_auth`. The dashboard accepts only loopback host names and the host of `dashboard.public_url`, and that host serves only the device routes, so you then use the dashboard UI over loopback or an SSH tunnel. Only `dashboard.basic_auth` works in this setup: redirect sign-in providers such as OAuth or OIDC send the browser back to `dashboard.public_url` plus `/auth/callback`, which the Hands host does not serve.
6. Restart the gateway, and start the dashboard on loopback, for example: `hermes dashboard --host 127.0.0.1 --port 9119 --no-open`. Restart both again whenever setup creates the seed.
7. For devices that reach the gateway over the internet, expose only the device pair and poll routes through nginx on the Hands host. Replace the example host and TLS certificate paths with your own existing certificate configuration.

```nginx
server {
    listen 443 ssl;
    server_name hands.example.com;
    ssl_certificate /path/to/existing/fullchain.pem;
    ssl_certificate_key /path/to/existing/privkey.pem;
    client_max_body_size 4m;

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

`client_max_body_size 4m;` lets a screenshot of up to 2 MiB through; without it nginx refuses any request body over 1 MiB. `server_name` must be the host of `dashboard.public_url`, because the dashboard refuses any other host name. Do not add the `return 404` location to a host that serves anything else. Keep the dashboard bound to loopback. Do not expose `/local/*` or `/ui/*` through the public route.

Then run `hermes hands doctor`. It checks the backend, that the relay answers at `dashboard_url`, `dashboard.public_url`, interactive dashboard authentication, and that the gateway and the dashboard use the same `HERMES_HOME`.

### Device

On Windows, in PowerShell:

1. Install Python and Git for Windows. Git for Windows provides Git Bash and its `cygpath` tool, which file deletes on a Windows device rely on:

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

   To keep the device's private key in the OS keyring instead of a file, install it with `pipx install "hermes-hands[keyring]==0.1.0"`.

4. Make `python3` work inside Git Bash. In Git Bash, `python3 -c "import sys; print(sys.version)"` must print a Python version. The python.org build from step 1 has no `python3.exe`, so Git Bash finds the Microsoft Store alias instead, which only prints "Python was not found". Installing the Python Install Manager gives Git Bash a working `python3`; on the laptop used for the live checks it runs the Python 3.12 from step 1. `hermes-hands doctor` runs the same check in Git Bash and names the Store alias if that is what it finds.

Pair using the command printed by `/hands pair laptop`, substituting the displayed URL, name, and code. Mint the code after the device is installed: it works once and expires after 10 minutes.

```powershell
hermes-hands pair https://hands.example.com --name laptop --code=ONE_TIME_CODE
hermes-hands serve
```

Run exactly one `hermes-hands serve` per device. Two agents for the same device fail every command. Screen capture is off by default; to opt in, run the agent as `hermes-hands serve --allow-screen`. To start the agent at logon instead, run `hermes-hands install-service`, which registers a Windows scheduled task that runs `hermes-hands serve`; add `--allow-screen` (`hermes-hands install-service --allow-screen`) to let that agent take screenshots. To change the flag later, remove the task with `hermes-hands install-service --remove` and install it again. Do not start a second `serve` next to the task to get screenshots. `hermes-hands install-service --dry-run` prints the planned action.

Linux devices need Python 3.11 or later, pipx, and a Bash environment. Install `hermes-hands==0.1.0` (or `"hermes-hands[keyring]==0.1.0"`), pair using the command printed by `/hands pair <name>`, then run `hermes-hands serve`. The service installer is implemented for Windows Task Scheduler.

Optional on Windows: `winget install BurntSushi.ripgrep.MSVC` enables ripgrep features in search. `hermes-hands doctor` reports whether Git Bash and Python are available inside it. The device needs Python on Git Bash's PATH for `execute_code` and some file operations.

In Git Bash, native Windows commands that take `/switches` need the slash doubled, as in `cmd //c dir` or `tasklist //FO CSV`, because Git Bash turns `/c` into a path. The bundled `hands` skill tells the model this.

## What runs on the device

With Hermes 0.23.0, Hands routes these tools through the selected device terminal backend: `terminal`, `execute_code`, `read_file`, `write_file`, `patch`, and `search_files`. `execute_code` needs Python available inside Git Bash. Hands also registers `hands_screenshot` and `hands_devices` as its own tools.

The target is selected for each terminal call. The first terminal result after a target change identifies the target and device home. Terminal commands on a device start in the device's home folder. File tools do not follow the device's working folder, so the bundled skill tells the model to give them absolute device paths. `search_files` patterns reach the device exactly as the model wrote them. `/hands use server` switches terminal calls back to the gateway host. A chat that never chose a target uses `default_target`, which defaults to `server`; cron execution always uses `server`. The target belongs to the chat, so `/new` and `/reset` keep it: send `/hands use server` first when a fresh conversation should run on the gateway. A device command never falls back to the gateway.

## Known limits

- Hermes takes no file checkpoints under `terminal.backend: hands`, on any target including `server`, and `/rollback` is refused.
- If a background job's pid is in use on two targets at once, `process kill` refuses that pid instead of guessing, until one of the jobs exits.
- If the gateway loses the relay's reply just after the relay queued a command, Hands reports the relay as unreachable and says nothing ran, although the command may still run on the device.
- When the device agent restarts, the gateway notices within about 10 seconds and then fails a command the old agent had taken, saying it may have partly run.
- A background job a device started with `nohup` keeps running after the device is revoked; stop it on the device yourself.
- The device deny list checks the whole command a tool sends, so a file read, write or search whose path or search pattern contains a listed word such as `sudo` or `shutdown` is refused with exit 126. Rename the path, or search with the terminal instead.
- A command handed to a connection the device can no longer read, for example during a Wi-Fi switch while the same agent process keeps running, is not delivered again; it fails when the gateway's terminal timeout ends.

## Security model

The relay is trusted; whoever controls the gateway and dashboard can command every paired device, and the signatures protect the path between them, not against a compromised gateway. Pairing and target switching from chat are owner gated. `/hands pair` and `/hands use <device>` work only in a direct chat with the bot, because a pairing code is a secret and a device target in a shared chat would apply to everyone in it; `/hands use server` works anywhere. When `owner_ids` is set, which `hermes hands setup` does, it alone decides who the owner is; only when it is empty does the platform's `allow_admin_from` list for that chat count. The device keeps its private signing key locally; the gateway stores device public keys. Requests have a 300 second freshness window and the relay's nonce cache keeps used nonces for 600 seconds. Pairing codes expire after 600 seconds and are single use.

Hermes's token seam offers every plugin token route to every token provider, and core's drain route checks neither the provider nor the scope. So a fresh bearer token signed by a paired or revoked device's key, a pairing code that is still unused, and the gateway's local bearer (derived from the relay seed, held by the gateway and the dashboard) also pass the token check on other plugins' token routes, such as the drain route on a Hermes install that enables it. A revoked device's key keeps that access until the device has been told to stop, a device with the same name is paired again, or 7 days pass. Hands cannot limit that from a plugin; it needs a change in Hermes core.

Hermes's existing approval gate runs first. A device policy deny list adds a second guard. The relay checks the device policy hash against the hash recorded at pairing; if it changes, the relay refuses work until the owner accepts the changed policy. The deny list matches the text of each command, so it is best effort: it stops the commands it lists as they are usually written, and a command written to avoid it can still get through. It is a guard rail, not a sandbox.

The agent runs as the logged in device user. A same-user shell can read or change any file that user can access, including credentials. The deny list cannot isolate the agent from that user's permissions.

The device connects out and does not open a listening port. Ed25519 signatures authenticate requests in both directions. They detect forged or replayed requests between the paired device and relay but do not protect against a compromised gateway.

## Stored data and retention

The gateway stores the relay seed through Hermes's declared secret configuration, and stores device public keys and pairing metadata in the plugin database. It does not store device private keys. The device stores its Ed25519 private key in the OS keyring only when the agent was installed with the `keyring` extra (`pipx install "hermes-hands[keyring]==0.1.0"`); otherwise, as with the plain install, it uses `~/.hermes-hands/device.json`. The keyring service is `hermes-hands` and the entry username is `device-private`. The fallback file is restricted to the device user: an owner only ACL on Windows, mode 0600 elsewhere.

Device output logs are written under `~/.hermes-hands/out/` and the device agent deletes them after 7 days. Hermes's temporary shell and cwd files on the device (`hermes-snap-*.sh` and `hermes-cwd-*.txt` in the system temp folder and in a local Hermes install's `cache/terminal` folder, if one exists) are removed after 24 hours. Both cleanups run when `serve` starts and then hourly while it runs. Delete output logs sooner with `rm -rf ~/.hermes-hands/out` or remove selected files there.

Screenshots are written on the gateway under `$HERMES_HOME/cache/hermes-hands/`. Each new screenshot first removes screenshots in that folder older than 24 hours, so the last ones stay until the next capture. Remove them sooner by deleting the files in that folder. A captured screenshot is sent to your configured vision model provider when the model describes it, and to the chat platform when it is delivered to the chat.

## Latency

About 0.4 s per small command (p50 409 ms, p90 411 ms over 30 runs, measured twice) from a Hermes v0.23.0 gateway in WSL to a Windows 11 laptop on the same machine, network latency excluded. Most of what remains is Hermes's own per command shell wrapper and its exit polling.

## Why a plugin, not a skill

1. A terminal backend core routes terminal and file tools through the selected environment.
2. An authenticated relay routes work to paired devices and handles pairing.
3. A process on the laptop receives signed work and enforces its own device policy.
4. Per conversation target selection and the Desktop pane let users choose and inspect the active device.

A skill can explain how to use a feature, but cannot register a terminal backend, provide authenticated relay routes, run a laptop process, or enforce local policy.

## Tests and validation

180 tests run against Hermes 0.23.0: 126 plugin tests, 6 of them end to end with the real device agent and the real relay in one process, and 54 device tests. On Windows 178 pass and 2 are skipped by design (one needs a Linux gateway, one is POSIX only). CI workflows for Ubuntu and Windows (plugin) and Ubuntu, Windows, and macOS (device) are in the repository but have not run yet, because the repository is not public yet.

The device CI tests on Python 3.11 and 3.12. Plugin CI validates the plugin and doctor, runs the plugin suite against Hermes 0.23.0, validates catalog shape, scans plugin payloads, and checks Python changes for Windows footguns.

## Not yet verified live

- Telegram interactions
- Screenshot capture and delivery
- The Desktop pane inside the app
- Linux devices
- macOS devices
- `hermes-hands install-service` (the Windows scheduled task)

This list will shrink as live tests land. Live checks on a real Hermes 0.23.0 gateway in WSL and a native Windows 11 laptop confirmed pairing, terminal work on the laptop, persistent working directory, a 200,001 byte `write_file` round trip verified by SHA-256, timeout handling, offline failure without execution, and recovery after the device restarted.

## Uninstall

On the device:

1. Stop the agent: press Ctrl+C in the terminal that runs `hermes-hands serve`, or, if the scheduled task started it, run `schtasks /End /TN HermesHands` in PowerShell. Then run `hermes-hands install-service --remove` if the task is installed.
2. Run `hermes-hands unpair`.
3. Remove `~/.hermes-hands`, and delete the keyring entry with service `hermes-hands` and username `device-private` if you installed the `keyring` extra.
4. Uninstall the package with `pipx uninstall hermes-hands`.

On the gateway:

5. Revoke the paired device with `hermes hands revoke laptop` (replace `laptop` with its name).
6. Set `terminal.backend` back to the backend you used before Hands, for example `hermes config set terminal.backend local`, and undo the placement line with `hermes config unset bot_desktop.placement`.
7. Remove the plugin with `hermes plugins remove hermes-hands`. This also removes its settings, `owner_ids` included.
8. Delete the relay seed with `hermes config unset HANDS_RELAY_SEED`.
9. Delete the plugin data and the screenshot cache: `$HERMES_HOME/plugin-data/hermes-hands/` and `$HERMES_HOME/cache/hermes-hands/`.
10. Restart the gateway and the dashboard.

## Install from Hermes

Before Hermes Hands is in the plugin catalog: [Install Hermes Hands](hermes://plugin/install?repo=Ridwannurudeen/hermes-hands/hermes-hands&enable=1)

After it is admitted, use the catalog link, which installs the reviewed commit: [Install Hermes Hands from the catalog](hermes://plugin/install?catalog=hermes-hands)

Repository: [Ridwannurudeen/hermes-hands](https://github.com/Ridwannurudeen/hermes-hands)

## License

MIT. See [LICENSE](LICENSE).
