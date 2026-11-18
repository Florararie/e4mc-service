"""``python -m e4mc_watcher`` -- mirror the tunnel's link onto GitHub Pages.

Both processes run on the same machine as the Minecraft server. The tunnel
writes the address it currently holds to ``STATE_DIR/state.json``; this watcher
reads that file, and publishes a landing page only when the visible address
changes. Nothing leaves the machine except the GitHub commit.

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
    commit_message: str

    @classmethod
    def from_env(cls) -> "WatcherConfig":
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
    padding: 3.5rem 1.25rem 2.5rem;
    background: var(--bg);
    color: var(--fg);
    font: 16px/1.6 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  }
  main { max-width: 33rem; margin: 0 auto; }
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
    body { padding-top: 2.25rem; }
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


def sync_page(config: WatcherConfig, content: bytes) -> bool:
    """Make ``PAGES_PATH`` equal ``content``; return True if a commit was made."""
    url = f"{GITHUB_API}/repos/{config.repo}/contents/{urllib.parse.quote(config.path)}"
    existing: dict | None
    try:
        existing = _api(config, "GET", f"{url}?ref={urllib.parse.quote(config.branch)}")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        existing = None

    payload: dict = {
        "message": config.commit_message,
        "content": base64.b64encode(content).decode("ascii"),
        "branch": config.branch,
    }
    if existing:
        payload["sha"] = existing.get("sha")
        try:
            current = base64.b64decode(existing.get("content") or "")
        except (ValueError, TypeError):
            current = b""
        if current == content:
            return False

    _api(config, "PUT", url, payload)
    return True


# -- main loop ---------------------------------------------------------------


def _report_http_error(exc: urllib.error.HTTPError) -> None:
    try:
        detail = exc.read().decode("utf-8", "replace")[:500]
    except Exception:
        detail = ""
    LOG.error("GitHub API returned HTTP %s: %s", exc.code, detail)


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
                        changed = sync_page(config, content)
                        LOG.info(
                            "%s now redirects from %s (%s)",
                            config.path,
                            domain,
                            "committed" if changed else "already current",
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
