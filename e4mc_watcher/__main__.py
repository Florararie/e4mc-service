"""``python -m e4mc_watcher`` -- mirror the tunnel's link onto GitHub Pages.

Both processes run on the same machine as the Minecraft server. The tunnel
writes the address it currently holds to ``STATE_DIR/state.json``; this watcher
reads that file, and publishes a landing page only when the visible address
changes. Nothing leaves the machine except the GitHub commit, which is amended
in place, so a service that runs for months still leaves one entry in the Pages
history.

A link is only trusted while it is fresh: ``connected`` must be true and the
heartbeat must be recent, so a watcher restarting next to a stopped or crashed
tunnel never republishes a dead address.
"""

from __future__ import annotations

import argparse
import base64
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

    @classmethod
    def from_env(cls) -> "WatcherConfig":
        commit_author_name, commit_author_email = parse_identity(
            _env("PAGES_COMMIT_AUTHOR", DEFAULT_COMMIT_AUTHOR)
        )
        return cls(
            state_dir=default_state_dir(),
            state_max_age=max(5.0, _env_float("STATE_MAX_AGE", 90.0)),
            token=_env("GITHUB_TOKEN"),
            repo=_env("PAGES_REPO"),
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
        )


# -- reading the link from the local tunnel ----------------------------------


def _age_seconds(stamp: object) -> float | None:
    if not isinstance(stamp, str) or not stamp.strip():
        return None
    try:
        moment = datetime.fromisoformat(stamp.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - moment).total_seconds())


def read_link(config: WatcherConfig) -> tuple[str | None, str]:
    """Return ``(domain, "")`` for a fresh link, or ``(None, reason)``."""
    path = config.state_dir / "state.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, f"no state at {path} yet"
    except (OSError, ValueError) as exc:
        return None, f"cannot read {path}: {exc}"
    if not isinstance(document, dict):
        return None, f"{path} is not a JSON object"
    if document.get("connected") is not True:
        return None, "the tunnel is not connected"

    age = _age_seconds(document.get("heartbeat_at") or document.get("updated_at"))
    if age is None:
        return None, "state has no usable timestamp"
    if age > config.state_max_age:
        return None, f"state is {age:.0f}s old (limit {config.state_max_age:.0f}s)"

    domain = normalize_domain(document.get("domain"), config.domain_suffix)
    if domain is None:
        return None, f"state holds an unusable domain: {document.get('domain')!r}"
    return domain, ""


# -- the landing page --------------------------------------------------------

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="e4mc-domain" content="__DOMAIN__">
<meta name="robots" content="noindex">
<meta http-equiv="refresh" content="60">
<title>__DOMAIN__ · Minecraft server</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #ffffff;
    --fg: #14161a;
    --muted: #5b616b;
    --panel: #f5f6f7;
    --line: #d8dbe0;
    --focus: #33663f;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #101215;
      --fg: #eceef1;
      --muted: #9aa1ab;
      --panel: #181b1f;
      --line: #2b3036;
      --focus: #8fd39b;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    padding: 2.5rem 1.25rem;
    min-height: 100vh;
    min-height: 100dvh;
    display: flex;
    flex-direction: column;
    justify-content: center;
    background: var(--bg);
    color: var(--fg);
    font: 16px/1.6 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  }
  main { width: 100%; max-width: 33rem; margin: 0 auto; }
  h1 { margin: 0 0 .35rem; font-size: 1.35rem; line-height: 1.3; letter-spacing: -.01em; }
  .lede { margin: 0 0 1.6rem; color: var(--muted); }
  .address {
    display: block;
    width: 100%;
    padding: 1rem 1.1rem;
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: .625rem;
    font: 600 1.125rem/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    overflow-wrap: anywhere;
    user-select: all;
  }
  .actions { margin: .75rem 0 0; }
  button {
    min-height: 2.75rem;
    padding: .55rem 1.15rem;
    font: inherit;
    font-weight: 600;
    color: var(--bg);
    background: var(--fg);
    border: 1px solid var(--fg);
    border-radius: .5rem;
    cursor: pointer;
  }
  button:hover { opacity: .86; }
  button:active { transform: translateY(1px); }
  button:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
  .note { margin: 1.4rem 0 0; color: var(--muted); font-size: .95rem; }
  .note code { font-size: .92em; }
  a { color: inherit; text-underline-offset: 2px; }
  footer {
    margin-top: 2.75rem;
    padding-top: 1.15rem;
    border-top: 1px solid var(--line);
    color: var(--muted);
    font-size: .85rem;
  }
  footer p { margin: .2rem 0; }
  @media (max-width: 26rem) {
    body { padding: 1.75rem 1.25rem; }
    .address { font-size: 1rem; }
  }
</style>
</head>
<body>
<main>
  <h1>Minecraft server</h1>
  <p class="lede">Paste this address into Minecraft to join.</p>

  <code class="address" id="address">__DOMAIN__</code>
  <p class="actions"><button id="copy" type="button">Copy address</button></p>

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
const address = document.getElementById("address");
const button = document.getElementById("copy");
const label = button.textContent.trim();
button.addEventListener("click", async () => {
  const text = address.textContent.trim();
  let copied = true;
  try {
    await navigator.clipboard.writeText(text);
  } catch (error) {
    copied = false;
    const range = document.createRange();
    range.selectNodeContents(address);
    const selection = getSelection();
    if (selection) {
      selection.removeAllRanges();
      selection.addRange(range);
    }
  }
  button.textContent = copied ? "Copied" : "Selected, press Ctrl/Cmd+C";
  setTimeout(() => { button.textContent = label; }, 2200);
});
</script>
</body>
</html>
"""


def render_page(domain: str, config: WatcherConfig) -> str:
    return (
        PAGE.replace("__DOMAIN__", escape(domain))
        .replace("__AUTHOR_PATH__", urllib.parse.quote(config.author, safe=""))
        .replace("__AUTHOR__", escape(config.author))
    )


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
    branch = urllib.parse.quote(config.branch, safe="/")
    return f"{GITHUB_API}/repos/{config.repo}/git/ref/heads/{branch}"


def _head_commit(config: WatcherConfig) -> dict | None:
    """Return the tip commit of the Pages branch, or None while it is unborn."""
    try:
        ref = _api(config, "GET", _branch_url(config))
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 409):
            return None
        raise
    sha = str((ref.get("object") or {}).get("sha") or "").strip()
    if not sha:
        return None
    commit = _api(config, "GET", f"{GITHUB_API}/repos/{config.repo}/git/commits/{sha}")
    commit["sha"] = sha
    return commit


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


def run(config: WatcherConfig, *, dry_run: bool = False, once: bool = False) -> int:
    last_domain: str | None = None
    last_reason: str | None = None
    while True:
        started = time.monotonic()
        try:
            domain, reason = read_link(config)
            if domain is None:
                if reason != last_reason:
                    LOG.info("holding the page: %s", reason)
                last_reason = reason
            else:
                last_reason = None
                if domain != last_domain:
                    content = render_page(domain, config).encode("utf-8")
                    if dry_run:
                        LOG.info("would serve %s for %s", domain, config.path)
                        print(content.decode("utf-8"))
                    else:
                        action = sync_page(config, content)
                        LOG.info(
                            "%s now shows %s (%s)",
                            config.path,
                            domain,
                            action or "already current",
                        )
                    last_domain = domain
                else:
                    LOG.debug("link unchanged: %s", domain)
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
        "--dry-run", action="store_true", help="print the page instead of committing it"
    )
    parser.add_argument("--log-level", default=_env("LOG_LEVEL", "INFO") or "INFO")
    args = parser.parse_args(argv)

    config = WatcherConfig.from_env()
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    if not args.dry_run:
        if not config.repo or "/" not in config.repo:
            raise SystemExit(
                "PAGES_REPO must be set to the Pages repository as owner/name "
                "(for example 'yourname/mc'); it is not fixed in the code. "
                "Use --dry-run to render the page without it."
            )
        if not config.token:
            raise SystemExit("GITHUB_TOKEN is required unless --dry-run is used")

    try:
        return run(config, dry_run=args.dry_run, once=args.once)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
