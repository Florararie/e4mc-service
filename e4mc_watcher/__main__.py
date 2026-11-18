"""``python -m e4mc_watcher`` -- mirror the tunnel's link onto Pages and/or Discord.

Both processes run on the same machine as the Minecraft server. The tunnel
writes the address it currently holds to ``STATE_DIR/state.json``; this watcher
reads that file and publishes the address wherever it has been told to: a
GitHub Pages landing page, a Discord webhook, both, or neither. The destinations
are independent, so a tunnel can be paired with any combination of them.

Nothing leaves the machine except those posts, and the page commit is amended in
place, so a service that runs for months still leaves one entry in the Pages
history.

A link is only *live* while it is fresh: ``connected`` must be true and the
heartbeat must be recent. A known-but-stale address is still shown, clearly
marked offline, so anyone opening the page can tell at a glance whether the
server is up right now.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from e4mc_tunnel.config import default_state_dir
from e4mc_tunnel.protocol import normalize_domain

LOG = logging.getLogger("e4mc.watcher")
USER_AGENT = "e4mc-watcher/2.0"
GITHUB_API = "https://api.github.com"
RECORD_NAME = "published.json"
DISCORD_LIVE_COLOR = 0x57F287
WEBHOOK_RETRY_SECONDS = 60.0


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be a number, got {raw!r}") from exc


DEFAULT_COMMIT_AUTHOR = "GitHub <noreply@github.com>"


def parse_identity(raw: str) -> tuple[str, str]:
    """Split ``Name <email>`` into the pair GitHub's commit API expects."""
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        raw = raw[1:-1].strip()
    name, separator, rest = raw.partition("<")
    email = rest.strip()
    if not separator or not name.strip() or not email.endswith(">") or "@" not in email:
        raise SystemExit(
            f'PAGES_COMMIT_AUTHOR must look like "Name <email>", got {raw!r}'
        )
    return name.strip(), email[:-1].strip()


@dataclass(frozen=True)
class WatcherConfig:
    state_dir: Path
    state_max_age: float
    token: str
    repo: str
    branch: str
    path: str
    poll_interval: float
    domain_suffix: str
    author: str
    commit_author_name: str
    commit_author_email: str
    commit_message: str
    webhook_url: str

    @property
    def pages_enabled(self) -> bool:
        return bool(self.repo and self.token)

    @property
    def webhook_enabled(self) -> bool:
        return bool(self.webhook_url)

    @property
    def publishing(self) -> bool:
        return self.pages_enabled or self.webhook_enabled

    @classmethod
    def from_env(cls) -> "WatcherConfig":
        commit_author_name, commit_author_email = parse_identity(
            _env("PAGES_COMMIT_AUTHOR", DEFAULT_COMMIT_AUTHOR)
        )
        repo = _env("PAGES_REPO")
        token = _env("GITHUB_TOKEN")
        webhook = _env("DISCORD_WEBHOOK_URL")
        if bool(repo) != bool(token):
            raise SystemExit(
                "GITHUB_TOKEN and PAGES_REPO must be set together (both or "
                "neither); PAGES_REPO is the repository as owner/name and the "
                "token needs Contents: Read and write on it."
            )
        if repo and "/" not in repo:
            raise SystemExit(
                f"PAGES_REPO must be the repository as owner/name, got {repo!r}"
            )
        if webhook and not webhook.startswith("https://"):
            raise SystemExit(
                "DISCORD_WEBHOOK_URL must be the https:// webhook link Discord "
                "gave you (Channel settings -> Integrations -> Webhooks)"
            )
        return cls(
            state_dir=default_state_dir(),
            state_max_age=max(5.0, _env_float("STATE_MAX_AGE", 90.0)),
            token=token,
            repo=repo,
            branch=_env("PAGES_BRANCH", "main") or "main",
            path=_env("PAGES_PATH", "index.html") or "index.html",
            poll_interval=max(1.0, _env_float("POLL_INTERVAL", 5.0)),
            domain_suffix=_env("DOMAIN_SUFFIX", ".e4mc.link") or ".e4mc.link",
            author=_env("PAGE_AUTHOR", "Florararie") or "Florararie",
            commit_author_name=commit_author_name,
            commit_author_email=commit_author_email,
            commit_message=_env(
                "PAGES_COMMIT_MESSAGE", "Update the Minecraft address on the page"
            )
            or "Update the Minecraft address on the page",
            webhook_url=webhook,
        )


# -- reading the link from the local tunnel ----------------------------------


def _stamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _age_seconds(stamp: object) -> float | None:
    moment = _stamp(stamp)
    if moment is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - moment).total_seconds())


def read_state(config: WatcherConfig) -> tuple[str | None, bool, str]:
    """Return ``(domain, live, reason)`` for the tunnel's current state.

    ``domain`` is the address the tunnel holds, even while it is disconnected;
    ``live`` says whether that address is usable right now. ``reason`` explains
    a ``None`` domain, and is empty in every other case.
    """
    path = config.state_dir / "state.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, False, f"no state at {path} yet"
    except (OSError, ValueError) as exc:
        return None, False, f"cannot read {path}: {exc}"
    if not isinstance(document, dict):
        return None, False, f"{path} is not a JSON object"

    domain = normalize_domain(document.get("domain"), config.domain_suffix)
    if domain is None:
        return None, False, f"state holds an unusable domain: {document.get('domain')!r}"
    if document.get("connected") is not True:
        return domain, False, ""

    age = _age_seconds(document.get("heartbeat_at") or document.get("updated_at"))
    if age is None:
        return domain, False, "state has no usable timestamp"
    if age > config.state_max_age:
        return domain, False, f"state is {age:.0f}s old (limit {config.state_max_age:.0f}s)"
    return domain, True, ""


# -- the landing page --------------------------------------------------------

OFFLINE_NOTE = (
    '<p class="note offline">The tunnel is offline right now. This is the last '
    "address it published, so joining may fail until the server is back and this "
    "page has refreshed.</p>"
)

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="e4mc-domain" content="__DOMAIN__">
<meta name="e4mc-status" content="__STATUS__">
<meta name="robots" content="noindex">
<meta http-equiv="refresh" content="60">
<title>__DOMAIN__ · Minecraft server</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #eef0f3;
    --card: #ffffff;
    --fg: #14161a;
    --muted: #5b616b;
    --panel: #f5f6f7;
    --line: #dfe3e8;
    --line-strong: #c6cdd6;
    --focus: #33663f;
    --ok: #16a34a;
    --ok-ring: rgba(22, 163, 74, .16);
    --ok-text: #15803d;
    --bad: #dc2626;
    --bad-ring: rgba(220, 38, 38, .16);
    --bad-text: #b91c1c;
    --shadow: 0 1px 2px rgba(16, 24, 40, .05), 0 18px 36px -14px rgba(16, 24, 40, .22);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0a0c0e;
      --card: #14171b;
      --fg: #eceef1;
      --muted: #9aa1ab;
      --panel: #1b1f24;
      --line: #2a2f36;
      --line-strong: #3c434d;
      --focus: #8fd39b;
      --ok: #4ade80;
      --ok-ring: rgba(74, 222, 128, .18);
      --ok-text: #86efac;
      --bad: #f87171;
      --bad-ring: rgba(248, 113, 113, .18);
      --bad-text: #fca5a5;
      --shadow: 0 1px 0 rgba(255, 255, 255, .03) inset, 0 24px 48px -18px rgba(0, 0, 0, .7);
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    padding: clamp(1.25rem, 4vw, 3rem);
    min-height: 100vh;
    min-height: 100dvh;
    display: flex;
    flex-direction: column;
    background: var(--bg);
    color: var(--fg);
    font: 16px/1.6 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  }
  main {
    width: 100%;
    max-width: 34rem;
    margin: auto;
    padding: clamp(1.5rem, 4.5vw, 2.4rem);
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 1.125rem;
    box-shadow: var(--shadow);
  }
  h1 { margin: 0 0 .5rem; font-size: 1.35rem; line-height: 1.3; letter-spacing: -.01em; }
  .status {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: .45rem;
    margin: 0 0 1.5rem;
    color: var(--muted);
    font-size: .9rem;
  }
  .status .state { font-weight: 600; }
  .status[data-state="live"] .state { color: var(--ok-text); }
  .status[data-state="offline"] .state { color: var(--bad-text); }
  .status .sep { opacity: .45; }
  .status time { font-variant-numeric: tabular-nums; }
  .dot {
    width: .55rem;
    height: .55rem;
    flex: none;
    border-radius: 50%;
    background: var(--muted);
  }
  .status[data-state="live"] .dot { background: var(--ok); box-shadow: 0 0 0 3px var(--ok-ring); }
  .status[data-state="offline"] .dot { background: var(--bad); box-shadow: 0 0 0 3px var(--bad-ring); }
  @media (prefers-reduced-motion: no-preference) {
    .status[data-state="live"] .dot { animation: pulse 2.4s ease-in-out infinite; }
    @keyframes pulse {
      0%, 100% { box-shadow: 0 0 0 3px var(--ok-ring); }
      50% { box-shadow: 0 0 0 7px rgba(74, 222, 128, 0); }
    }
  }
  .lede { margin: 0 0 1.6rem; color: var(--muted); }
  .address-bar {
    position: relative;
    display: flex;
    align-items: center;
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: .625rem;
    transition: border-color .15s ease, box-shadow .15s ease;
  }
  .address-bar:hover,
  .address-bar:focus-within { border-color: var(--line-strong); }
  .address-bar[data-state="offline"] { border-style: dashed; }
  .address {
    flex: 1;
    min-width: 0;
    padding: 1rem 3.15rem 1rem 1.1rem;
    font: 600 1.125rem/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    overflow-wrap: anywhere;
    user-select: all;
  }
  .address-bar[data-state="offline"] .address { color: var(--muted); }
  .copy {
    position: absolute;
    top: 50%;
    right: .55rem;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 2.1rem;
    height: 2.1rem;
    padding: 0;
    transform: translateY(-50%);
    color: var(--muted);
    background: transparent;
    border: 1px solid transparent;
    border-radius: .45rem;
    cursor: pointer;
    opacity: 0;
    transition: opacity .16s ease, background-color .16s ease, border-color .16s ease,
                color .16s ease, transform .1s ease;
  }
  .address-bar:hover .copy,
  .address-bar:focus-within .copy,
  .address-bar[data-feedback] .copy { opacity: 1; }
  .copy:hover { color: var(--fg); background: var(--card); border-color: var(--line-strong); }
  .copy:active { transform: translateY(-50%) scale(.94); }
  .copy:focus-visible { opacity: 1; outline: 2px solid var(--focus); outline-offset: 2px; }
  .copy svg { display: block; width: 1.05rem; height: 1.05rem; }
  .copy .icon-check { display: none; }
  .address-bar[data-feedback="copied"] .copy {
    color: var(--ok-text);
    background: var(--ok-ring);
    border-color: transparent;
  }
  .address-bar[data-feedback="copied"] .icon-copy { display: none; }
  .address-bar[data-feedback="copied"] .icon-check { display: block; }
  .copy-tip {
    position: absolute;
    right: .35rem;
    bottom: calc(100% + .4rem);
    padding: .3rem .55rem;
    font-size: .78rem;
    font-weight: 600;
    line-height: 1.25;
    white-space: nowrap;
    color: var(--card);
    background: var(--fg);
    border-radius: .35rem;
    opacity: 0;
    transform: translateY(.2rem);
    pointer-events: none;
    transition: opacity .16s ease, transform .16s ease;
  }
  .address-bar[data-feedback] .copy-tip { opacity: 1; transform: none; }
  @media (hover: none) {
    .copy { opacity: .6; }
  }
  .note { margin: 1.4rem 0 0; color: var(--muted); font-size: .95rem; }
  .note code { font-size: .92em; }
  .note.offline {
    margin-top: 1.2rem;
    padding-left: .85rem;
    border-left: 2px solid var(--bad);
  }
  a { color: inherit; text-underline-offset: 2px; }
  footer {
    margin-top: 2.4rem;
    padding-top: 1.15rem;
    border-top: 1px solid var(--line);
    color: var(--muted);
    font-size: .85rem;
  }
  footer p { margin: .2rem 0; }
  @media (max-width: 26rem) {
    body { padding: 1rem .9rem; }
    main { padding: 1.35rem 1.2rem; }
    .address { font-size: 1rem; }
  }
</style>
</head>
<body>
<main>
  <h1>Minecraft server</h1>
  <p class="status" data-state="__STATUS__">
    <span class="dot" aria-hidden="true"></span>
    <span class="state">__STATUS_LABEL__</span>
    <span class="sep" aria-hidden="true">&middot;</span>
    <span class="updated">Last changed
    <time datetime="__CHANGED_ISO__" title="__CHANGED_HUMAN__" data-relative>__CHANGED_HUMAN__</time></span>
  </p>
  <p class="lede">Paste this address into Minecraft to join.</p>

  <div class="address-bar" id="address-bar" data-state="__STATUS__">
    <code class="address" id="address">__DOMAIN__</code>
    <span class="copy-tip" id="copy-tip" role="status"></span>
    <button class="copy" id="copy" type="button" aria-label="Copy server address">
      <svg class="icon-copy" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">
        <rect x="9" y="9" width="12" height="12" rx="2"></rect>
        <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>
      </svg>
      <svg class="icon-check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">
        <path d="M4 12.5l5 5L20 6.5"></path>
      </svg>
    </button>
  </div>
  __OFFLINE_NOTE__

  <p class="note"><strong>Multiplayer &rarr; Direct Connection</strong>, paste,
  then <em>Join Server</em>. Use the address, not this page's URL: Minecraft
  looks up a hostname, so <code>https://</code> and anything after a
  <code>/</code> cannot work.</p>
  <p class="note">The address changes whenever the server reconnects, so reload
  this page if a join fails.</p>

  <footer>
    <p>Page and tunnel runner by
    <a href="https://github.com/__AUTHOR_PATH__">__AUTHOR__</a>.</p>
    <p>Tunnel by <a href="https://github.com/vgskye/e4mc-minecraft-architectury">e4mc</a>
    (<a href="https://github.com/vgskye/e4mc-quiclime">relay</a>). Not an official
    Minecraft service.</p>
  </footer>
</main>
<script>
const bar = document.getElementById("address-bar");
const address = document.getElementById("address");
const copy = document.getElementById("copy");
const tip = document.getElementById("copy-tip");
let feedbackTimer;

const feedback = (state, message) => {
  bar.dataset.feedback = state;
  tip.textContent = message;
  clearTimeout(feedbackTimer);
  feedbackTimer = setTimeout(() => {
    delete bar.dataset.feedback;
    tip.textContent = "";
    copy.setAttribute("aria-label", "Copy server address");
  }, 2000);
};

const copyAddress = async () => {
  const text = address.textContent.trim();
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (error) {
    /* fall through to selecting the text */
  }
  try {
    const range = document.createRange();
    range.selectNodeContents(address);
    const selection = getSelection();
    if (selection) {
      selection.removeAllRanges();
      selection.addRange(range);
    }
    return document.execCommand("copy");
  } catch (error) {
    return false;
  }
};

copy.addEventListener("click", async () => {
  if (await copyAddress()) {
    feedback("copied", "Copied");
    copy.setAttribute("aria-label", "Address copied");
  } else {
    feedback("manual", "Press Ctrl/Cmd+C");
    copy.setAttribute("aria-label", "Address selected, press Ctrl/Cmd+C");
  }
});

const stamp = document.querySelector("time[data-relative]");
if (stamp) {
  const when = new Date(stamp.getAttribute("datetime"));
  const units = [
    ["year", 31557600], ["month", 2629800], ["week", 604800],
    ["day", 86400], ["hour", 3600], ["minute", 60]
  ];
  const phrase = () => {
    const seconds = Math.max(0, (Date.now() - when.getTime()) / 1000);
    if (!isFinite(seconds) || seconds < 45) return "just now";
    for (const [unit, size] of units) {
      if (seconds >= size) {
        const count = Math.round(seconds / size);
        return count + " " + unit + (count === 1 ? "" : "s") + " ago";
      }
    }
    return "just now";
  };
  const paint = () => { stamp.textContent = phrase(); };
  paint();
  setInterval(paint, 30000);
}
</script>
</body>
</html>
"""


def render_page(
    domain: str, config: WatcherConfig, *, live: bool, changed_at: datetime
) -> str:
    changed_at = changed_at.astimezone(timezone.utc)
    return (
        PAGE.replace("__STATUS__", "live" if live else "offline")
        .replace("__STATUS_LABEL__", "Live" if live else "Offline")
        .replace("__CHANGED_ISO__", changed_at.strftime("%Y-%m-%dT%H:%M:%SZ"))
        .replace("__CHANGED_HUMAN__", changed_at.strftime("%d %b %Y, %H:%M UTC"))
        .replace("__OFFLINE_NOTE__", "" if live else OFFLINE_NOTE)
        .replace("__DOMAIN__", escape(domain))
        .replace("__AUTHOR_PATH__", urllib.parse.quote(config.author, safe=""))
        .replace("__AUTHOR__", escape(config.author))
    )


# -- remembering what was already published ----------------------------------


def _record_path(config: WatcherConfig) -> Path:
    return config.state_dir / RECORD_NAME


def _load_record(config: WatcherConfig) -> dict:
    try:
        data = json.loads(_record_path(config).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_record(config: WatcherConfig, record: dict) -> None:
    path = _record_path(config)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)
    except OSError as exc:
        LOG.warning("cannot record the published state in %s: %s", path, exc)


# -- Discord webhook ---------------------------------------------------------


def _post_webhook(config: WatcherConfig, domain: str) -> bool:
    """Post one address to the configured webhook; False means retry later."""
    payload = {
        "username": "e4mc",
        "allowed_mentions": {"parse": []},
        "embeds": [
            {
                "title": "Minecraft server is live",
                "description": f"`{domain}`",
                "color": DISCORD_LIVE_COLOR,
                "footer": {"text": "e4mc tunnel"},
            }
        ],
    }
    request = urllib.request.Request(
        config.webhook_url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            detail = ""
        LOG.warning("Discord refused the webhook post (HTTP %s): %s", exc.code, detail)
        return False
    except Exception as exc:  # network trouble: try again on a later poll
        LOG.warning("Discord webhook post failed: %s", exc)
        return False
    LOG.info("posted %s to the Discord webhook", domain)
    return True


# -- GitHub Pages ------------------------------------------------------------


def _api(config: WatcherConfig, method: str, url: str, payload: dict | None = None) -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {config.token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
    }
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read()
    return json.loads(body) if body else {}


def _branch_url(config: WatcherConfig) -> str:
    """The reference this watcher updates (``git/refs`` is the write path)."""
    branch = urllib.parse.quote(config.branch, safe="/")
    return f"{GITHUB_API}/repos/{config.repo}/git/refs/heads/{branch}"


def _head_commit(config: WatcherConfig) -> dict | None:
    """Return the tip commit of the Pages branch, or None while it is unborn.

    Read through the commits API rather than the git-data ``git/ref`` endpoint:
    that is the one read a token with Contents access is always granted, and a
    token refused there still publishes the page.
    """
    branch = urllib.parse.quote(config.branch, safe="/")
    try:
        data = _api(config, "GET", f"{GITHUB_API}/repos/{config.repo}/commits/{branch}")
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 409):
            return None
        raise
    sha = str(data.get("sha") or "").strip()
    if not sha:
        return None
    commit = data.get("commit") or {}
    return {
        "sha": sha,
        "message": commit.get("message") or "",
        "tree": {"sha": (commit.get("tree") or {}).get("sha")},
        "parents": list(data.get("parents") or []),
    }


def _contents_url(config: WatcherConfig) -> str:
    return f"{GITHUB_API}/repos/{config.repo}/contents/{urllib.parse.quote(config.path)}"


def _published_copy(config: WatcherConfig) -> dict | None:
    """Return the file currently on the branch, or None when it is absent."""
    branch = urllib.parse.quote(config.branch, safe="/")
    try:
        return _api(config, "GET", f"{_contents_url(config)}?ref={branch}")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        return None


def _decoded_contents(entry: dict | None) -> bytes:
    try:
        return base64.b64decode((entry or {}).get("content") or "")
    except (ValueError, TypeError):
        return b""


def sync_page(config: WatcherConfig, content: bytes) -> str | None:
    """Make ``PAGES_PATH`` equal ``content``.

    Returns None when the published page is already identical, otherwise
    ``"amended"``, ``"committed"`` or ``"written"``. The watcher keeps its own
    tip commit amended, so an installation that runs indefinitely still leaves
    one entry in the Pages history. A commit it did not write is never
    rewritten: the page commit is then simply added on top of it. A token that
    cannot use the git-data endpoints falls back to a plain contents commit,
    which keeps the page current but appends instead of amending.
    """
    existing = _published_copy(config)
    if existing is not None and _decoded_contents(existing) == content:
        return None
    try:
        return _publish_commit(config, content)
    except urllib.error.HTTPError as exc:
        if exc.code not in (401, 403, 404):
            raise
        _report_http_error(exc)
        LOG.warning(
            "falling back to a plain contents commit; history will grow again "
            "until the git-data endpoints are allowed"
        )
        return _publish_contents(config, content, existing)


def _publish_contents(
    config: WatcherConfig, content: bytes, existing: dict | None
) -> str:
    payload: dict = {
        "message": config.commit_message,
        "content": base64.b64encode(content).decode("ascii"),
        "branch": config.branch,
    }
    if existing:
        payload["sha"] = existing.get("sha")
    _api(config, "PUT", _contents_url(config), payload)
    return "written"


def _publish_commit(config: WatcherConfig, content: bytes) -> str:
    """Create the page commit through the git-data API, amending when possible."""
    head = _head_commit(config)
    # Only rewrite a commit this watcher wrote itself, identified by its
    # message; anything else (your own edits, a merge) gets a normal commit on
    # top, so nothing published by hand is ever discarded.
    amend = bool(
        head
        and len(head.get("parents") or []) <= 1
        and str(head.get("message") or "").strip() == config.commit_message.strip()
    )

    blob = _api(
        config,
        "POST",
        f"{GITHUB_API}/repos/{config.repo}/git/blobs",
        {"content": content.decode("utf-8"), "encoding": "utf-8"},
    )
    tree_payload: dict = {
        "tree": [
            {
                "path": config.path,
                "mode": "100644",
                "type": "blob",
                "sha": blob["sha"],
            }
        ]
    }
    if head and (head.get("tree") or {}).get("sha"):
        tree_payload["base_tree"] = head["tree"]["sha"]
    tree = _api(
        config, "POST", f"{GITHUB_API}/repos/{config.repo}/git/trees", tree_payload
    )

    identity = {"name": config.commit_author_name, "email": config.commit_author_email}
    if head is None:
        parents: list[str] = []
    elif amend:
        parents = [
            str((parent or {}).get("sha") or "")
            for parent in head.get("parents") or []
        ]
    else:
        parents = [str(head["sha"])]

    commit = _api(
        config,
        "POST",
        f"{GITHUB_API}/repos/{config.repo}/git/commits",
        {
            "message": config.commit_message,
            "tree": tree["sha"],
            "parents": [sha for sha in parents if sha],
            "author": identity,
            "committer": identity,
        },
    )

    if head is None:
        _api(
            config,
            "POST",
            f"{GITHUB_API}/repos/{config.repo}/git/refs",
            {"ref": f"refs/heads/{config.branch}", "sha": commit["sha"]},
        )
        return "committed"

    # Rewriting the tip is a force update, so refuse if somebody else moved the
    # branch between our read and our write.
    fresh = _head_commit(config)
    if fresh is None or fresh.get("sha") != head.get("sha"):
        raise RuntimeError(
            "the branch moved while publishing; will retry on the next poll"
        )
    _api(
        config,
        "PATCH",
        _branch_url(config),
        {"sha": commit["sha"], "force": amend},
    )
    return "amended" if amend else "committed"


# -- main loop ---------------------------------------------------------------


def _report_http_error(exc: urllib.error.HTTPError) -> None:
    try:
        detail = exc.read().decode("utf-8", "replace")[:500]
    except Exception:
        detail = ""
    LOG.error(
        "GitHub API returned HTTP %s for %s: %s",
        exc.code,
        getattr(exc, "url", "?"),
        detail,
    )


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def run(config: WatcherConfig, *, dry_run: bool = False, once: bool = False) -> int:
    last_reason: str | None = None
    webhook_retry_at = 0.0
    first_poll = True
    while True:
        started = time.monotonic()
        try:
            domain, live, reason = read_state(config)
            if domain is None:
                if reason and reason != last_reason:
                    LOG.info("nothing to publish yet: %s", reason)
                last_reason = reason or None
            else:
                last_reason = None
                record = _load_record(config)
                dirty = False
                if record.get("domain") != domain:
                    record["domain"] = domain
                    record["changed_at"] = _now().isoformat()
                    dirty = True
                changed_at = _stamp(record.get("changed_at")) or _now()

                page = render_page(domain, config, live=live, changed_at=changed_at)
                content = page.encode("utf-8")
                if dry_run:
                    LOG.info(
                        "would serve %s for %s (%s)",
                        domain,
                        config.path,
                        "live" if live else "offline",
                    )
                    print(page)
                else:
                    digest = hashlib.sha256(content).hexdigest()
                    if config.pages_enabled and (
                        first_poll or record.get("published_sha256") != digest
                    ):
                        action = sync_page(config, content)
                        record["published_sha256"] = digest
                        dirty = True
                        LOG.info(
                            "%s now shows %s (%s, %s)",
                            config.path,
                            domain,
                            "live" if live else "offline",
                            action or "already current",
                        )
                    if (
                        config.webhook_enabled
                        and live
                        and record.get("webhook_domain") != domain
                        and time.monotonic() >= webhook_retry_at
                    ):
                        if _post_webhook(config, domain):
                            record["webhook_domain"] = domain
                            dirty = True
                        else:
                            webhook_retry_at = time.monotonic() + WEBHOOK_RETRY_SECONDS
                    if dirty:
                        _save_record(config, record)
                    LOG.debug("poll: %s (%s)", domain, "live" if live else "offline")
                first_poll = False
        except urllib.error.HTTPError as exc:
            _report_http_error(exc)
        except Exception as exc:
            LOG.warning("poll failed: %s", exc)

        if once:
            return 0
        elapsed = time.monotonic() - started
        time.sleep(max(0.5, config.poll_interval - elapsed))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="run a single poll and exit")
    parser.add_argument(
        "--dry-run", action="store_true", help="print the page instead of publishing it"
    )
    parser.add_argument("--log-level", default=_env("LOG_LEVEL", "INFO") or "INFO")
    args = parser.parse_args(argv)

    config = WatcherConfig.from_env()
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    if not config.publishing and not args.dry_run:
        LOG.warning(
            "nothing to publish: set PAGES_REPO and GITHUB_TOKEN for a page, "
            "DISCORD_WEBHOOK_URL for a webhook, or both; only link changes "
            "will be logged"
        )

    try:
        return run(config, dry_run=args.dry_run, once=args.once)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
