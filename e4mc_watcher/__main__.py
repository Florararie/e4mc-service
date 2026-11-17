"""``python -m e4mc_watcher`` -- mirror the tunnel's link onto GitHub Pages.

Both processes run on the same machine as the Minecraft server. The tunnel
writes the address it currently holds to ``STATE_DIR/state.json``; this watcher
reads that file, and commits a redirect page only when the visible address
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
    redirect_template: str
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
            redirect_template=_env("REDIRECT_URL_TEMPLATE", "minecraft://{domain}")
            or "minecraft://{domain}",
            commit_message=_env(
                "PAGES_COMMIT_MESSAGE", "Point the redirect at the current e4mc link"
            )
            or "Point the redirect at the current e4mc link",
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


# -- the redirect page -------------------------------------------------------


def render_page(domain: str, config: WatcherConfig) -> str:
    try:
        target = config.redirect_template.format(domain=domain)
    except (KeyError, IndexError) as exc:
        raise SystemExit(f"REDIRECT_URL_TEMPLATE must only use {{domain}}: {exc}") from exc
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="e4mc-domain" content="{escape(domain)}">
<meta http-equiv="refresh" content="0; url={escape(target)}">
<title>Minecraft server</title>
<script>location.replace({json.dumps(target)});</script>
</head>
<body>
<p>This page points at <strong>{escape(domain)}</strong>.</p>
<p>If nothing opens, add <code>{escape(domain)}</code> as a server in Minecraft.</p>
</body>
</html>
"""


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
