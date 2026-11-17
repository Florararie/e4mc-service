"""Atomic on-disk state.

``state.json`` is the machine-readable record, ``link.txt`` the bare address.
Both are replaced atomically so a reader (or another watcher on the same host)
never observes a half-written link. A state write must never take the tunnel
down, so failures are logged and ignored.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

LOG = logging.getLogger("e4mc.state")


class StateStore:
    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def write(self, payload: dict) -> None:
        body = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        link = f"{payload.get('domain') or ''}\n"
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            self._replace("state.json", body)
            self._replace("link.txt", link)
        except OSError as exc:
            LOG.warning("could not write state to %s: %s", self.directory, exc)

    def _replace(self, name: str, text: str) -> None:
        target = self.directory / name
        temporary = target.with_name(f".{name}.{os.getpid()}.tmp")
        temporary.write_text(text, encoding="utf-8")
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)
