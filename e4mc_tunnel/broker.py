"""Relay discovery.

The mod asks ``https://broker.e4mc.link/getBestRelay`` for the nearest relay and
gets back ``{"id", "host", "port"}``. RELAY_HOST/RELAY_PORT bypass the broker.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from dataclasses import dataclass

from .config import TunnelConfig

LOG = logging.getLogger("e4mc.broker")
USER_AGENT = "e4mc-tunnel/1.0 (+https://github.com/Florararie/mc)"


@dataclass(frozen=True)
class Relay:
    id: str
    host: str
    port: int


def fetch_relay(config: TunnelConfig) -> Relay:
    if config.relay_host:
        return Relay(id="custom", host=config.relay_host, port=config.relay_port or 25575)

    request = urllib.request.Request(
        config.broker_url,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    LOG.debug("asking broker %s", config.broker_url)
    with urllib.request.urlopen(request, timeout=20) as response:
        if response.status != 200:
            raise RuntimeError(f"broker returned HTTP {response.status}")
        payload = json.load(response)

    try:
        host = str(payload["host"])
        port = int(payload["port"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"unexpected broker response: {payload!r}") from exc

    LOG.info("broker picked relay %s at %s:%s", payload.get("id", "?"), host, port)
    return Relay(id=str(payload.get("id", "unknown")), host=host, port=port)
