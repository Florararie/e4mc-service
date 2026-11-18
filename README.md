# e4mc headless tunnel + Pages link

Exposes a Minecraft server through the public [e4mc](https://e4mc.link) relay
without opening a port, and keeps a static redirect page pointed at whatever
address the relay currently hands out.

Both halves run on the same machine as the Minecraft server:

```
Minecraft server <--TCP--> e4mc tunnel <--QUIC--> public relay <---> players
                                |
                         state.json (local disk)
                                |
                          link watcher --HTTPS--> GitHub Pages --> https://<owner>.github.io/<repo>/
```

The tunnel is an ordinary outbound client: no mod, no port forward, no inbound
firewall rule. The watcher learns the link from the tunnel's state file on the
same disk, so the only thing that leaves the machine is the commit that updates
the redirect page.

## What the relay actually does

The behaviour below is reimplemented from the e4mc mod
(`QuiclimeSession.java`) and its relay (`e4mc-quiclime`), not guessed:

1. The broker at `https://broker.e4mc.link/getBestRelay` returns
   `{"id", "host", "port"}` for the nearest relay.
2. The client opens one QUIC connection with ALPN `quiclime` (the relay's
   certificate is not validated, matching the mod).
3. The first client-initiated bidirectional stream is the control channel:
   `varint(len) + JSON` frames carrying `probe_capabilities`,
   `request_domain_assignment`, `has_capabilities`, and finally
   `domain_assignment_complete` with an address such as
   `amber-river.na.e4mc.link`.
4. That address is wildcard DNS for the relay. A player joining it makes the
   relay parse the hostname in the Minecraft handshake, look up the tunnel, and
   hand the player to the client on a **server-initiated** QUIC stream as raw
   Minecraft TCP bytes.
5. The tunnel splices each such stream into `MC_HOST:MC_PORT`. A Minecraft LAN
   world is never involved; the dedicated server keeps serving normally.

Keepalive pings run every 15 s. If the connection dies, the relay drops the
routing entry, so the address is gone: the supervisor reconnects with
exponential backoff, obtains a **new** address, and rewrites the state file.
The watcher notices and commits the new redirect.

## Layout

| Path | Purpose |
| --- | --- |
| `e4mc_tunnel/` | QUIC client, per-player stream proxy, atomic `state.json`/`link.txt`, keepalive, heartbeat, backoff reconnect |
| `e4mc_watcher/` | Reads the link from local state and commits the redirect page through the GitHub Contents API |
| `deploy/*.service` | Unit templates (`@PREFIX@`, `@USER@`, `@STATE_DIR@` substituted at install time) |
| `install.sh` | Installs both services, the venv, the env file and the units |
| `uninstall.sh` | Stops and removes both services; `--purge` also deletes config and state |
| `.env.example` | Every setting with its default |

## Install (systemd, both services on one machine)

```bash
git clone https://github.com/Florararie/e4mc-service && cd e4mc-service
printf '%s\n' 'github_pat_your_token_here' > token.txt && chmod 600 token.txt
sudo ./install.sh --repo YourName/YourRepo --token-file token.txt
```

`install.sh` creates a system account (`e4mc` by default), installs the code
under `/opt/e4mc`, builds the venv, writes `/etc/e4mc/e4mc.env` (mode 0600),
renders the units, and starts both services.

Options (`sudo ./install.sh --help` lists them all):

| Option | Default | Meaning |
| --- | --- | --- |
| `--repo OWNER/NAME` | required | repository whose Pages site gets the redirect |
| `--token-file PATH` / `--token VALUE` | required | token with Contents: Read and write |
| `--prefix DIR` | `/opt/e4mc` | install directory |
| `--state-dir DIR` | `/var/lib/e4mc` | shared state directory |
| `--user NAME` | `e4mc` | service account, created if absent |
| `--mc-host HOST` / `--mc-port PORT` | `127.0.0.1` / `25565` | where the server listens |
| `--branch NAME` | `main` | Pages branch |
| `--pages-path PATH` | `index.html` | file the watcher commits |
| `--force` | off | rewrite an existing env file |
| `--no-start` | off | enable but do not start |

Re-running the installer updates the code and keeps the existing env file
unless `--force` is passed.

## Uninstall

```bash
sudo ./uninstall.sh                  # stop + disable, remove units and code
sudo ./uninstall.sh --dry-run        # print exactly what would be removed
sudo ./uninstall.sh --purge          # also remove env file, state and account
sudo ./uninstall.sh --keep-code      # undo the services, leave the install
```

The installer copies `uninstall.sh` to the prefix, so
`sudo /opt/e4mc/uninstall.sh` works too. Without `--purge`, the token in
`/etc/e4mc/e4mc.env` and the last link in the state directory are kept for a
reinstall. `--purge` only deletes the account when it is a system account with
a `nologin`/`false` shell; a real login account is reported and left alone.
Neither mode touches the GitHub repository or the published `index.html`.
Options: `--prefix`, `--state-dir`, `--user`, `--keep-code`, `--purge`,
`--dry-run`.

Nothing is tied to a particular repository, account or path: the repository is
an option, the units are templates (prefix, account, state directory), and the
env file is shared by both services because they run together.

## Manual run (no systemd)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env            # edit: GITHUB_TOKEN, PAGES_REPO, MC_PORT
set -a && . ./.env && set +a

.venv/bin/python -m e4mc_tunnel        # terminal 1: keeps the tunnel up
.venv/bin/python -m e4mc_watcher       # terminal 2: keeps the page current
```

Useful watcher flags: `--once` (single reconciliation), `--dry-run` (print the
page instead of committing), `--log-level DEBUG`.

## How the two halves talk

The tunnel atomically replaces `STATE_DIR/state.json`:

```json
{
  "connected": true,
  "domain": "amber-river.na.e4mc.link",
  "heartbeat_at": "2026-11-17T18:13:45+00:00",
  "relay": "na",
  "updated_at": "2026-11-17T18:13:45+00:00",
  "version": 2
}
```

and writes the bare address to `STATE_DIR/link.txt` for scripts.

The watcher only trusts a link when `connected` is true and `heartbeat_at` is
younger than `STATE_MAX_AGE` (90 s by default). The tunnel refreshes the
heartbeat every `HEARTBEAT_INTERVAL` (20 s) and marks the state disconnected
when a connection ends. This matters at boot: if the watcher starts before the
tunnel has reconnected, it leaves the old page alone instead of publishing a
dead address.

The commit only happens when the rendered page differs from the file already in
the repository, so a stable link produces no commits.

## GitHub setup

1. Create a repository for the page (public, or private with Pages on a paid
   plan) and enable **Settings → Pages → Deploy from a branch → `main` / root**.
2. Create a fine-grained token scoped to that repository with
   **Contents: Read and write**. No other scopes are needed.
3. Point `--repo` at it; the watcher creates `index.html` on its first run and
   Pages serves it about a minute later.

The page asks the browser to open `minecraft://<address>` (meta refresh and
script) and shows the bare address with a copy button. Browsers usually cannot
launch Minecraft from a web page, so the printed address is the reliable path;
`REDIRECT_URL_TEMPLATE` lets you change the scheme.

### The Pages URL is not a server address

`https://<owner>.github.io/<repo>/` serves a web page, and Minecraft's Direct
Connection never fetches it: what you type is taken as a hostname and looked up
in DNS. No Pages URL can work, at the root or under a path, and an HTTP
redirect cannot help, because the client never makes an HTTP request. DNS is
the only layer that can redirect a Minecraft connection.

| Typed into Direct Connection | What happens |
| --- | --- |
| `https://owner.github.io/repo` | invalid address, so Join Server stays greyed out |
| `owner.github.io/repo` | `/repo` is read as part of the hostname, so DNS fails: "Unknown host" |
| `word-word.na.e4mc.link` | correct: the relay's hostname |

The Pages site is where players look the address up; the address itself is what
they paste. Moving the page into an `owner.github.io` repository does not help
(the root hostname resolves to GitHub's web servers, not to the relay), and
doing it in the repository that also publishes a resource pack is a bad idea:
the watcher would rewrite `index.html` there and the pack's force-push would
overwrite that commit.

## Configuration

Everything lives in `/etc/e4mc/e4mc.env` (or the environment when run by hand).
See `.env.example` for the annotated list. The ones that usually matter:

| Variable | Default | Effect |
| --- | --- | --- |
| `MC_HOST`, `MC_PORT` | `127.0.0.1`, `25565` | local server the players are proxied to |
| `STATE_DIR` | `/var/lib/e4mc` | shared by both services; set by `--state-dir` |
| `BROKER_URL` | e4mc broker | pin a different broker |
| `RELAY_HOST`, `RELAY_PORT` | unset, `25575` | skip the broker and pin a relay |
| `DOMAIN_SUFFIX` | `.e4mc.link` | accepted address suffix |
| `KEEPALIVE_INTERVAL`, `IDLE_TIMEOUT` | `15`, `30` | tunnel liveness |
| `RECONNECT_MIN`, `RECONNECT_MAX` | `1`, `30` | backoff bounds |
| `HEARTBEAT_INTERVAL` | `20` | state refresh while connected |
| `GITHUB_TOKEN`, `PAGES_REPO` | none | required for commits |
| `PAGES_BRANCH`, `PAGES_PATH` | `main`, `index.html` | where the page lives |
| `POLL_INTERVAL` | `5` | how often the watcher reads local state |
| `STATE_MAX_AGE` | `90` | freshness required before publishing |
| `REDIRECT_URL_TEMPLATE` | `minecraft://{domain}` | page target |

## Verify

```bash
systemctl status e4mc-tunnel e4mc-watcher
journalctl -u e4mc-tunnel -f          # "public link: word-word.na.e4mc.link"
cat /var/lib/e4mc/link.txt
curl -sL https://<owner>.github.io/<repo> | grep e4mc-domain

# from another machine, with the address printed above:
mcstatus word-word.na.e4mc.link status
```

## Limitations

* The address changes on every reconnect. Anything that cached the old page or
  address will need the new one; the Pages build adds about a minute.
* The relay and its wildcard DNS are third-party infrastructure. If e4mc is
  down, the tunnel cannot obtain a link; the watcher keeps the last page but
  marks nothing as connected.
* The GitHub token must be able to write the repository; a read-only or
  expired token keeps retrying and logs the API error each poll.
* One install serves one address. A second Minecraft server needs its own copy
  with renamed units and a separate state directory.
* A GitHub Pages URL can never be typed into Minecraft, only the bare e4mc
  hostname works; the page exists to display it.
