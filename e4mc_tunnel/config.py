"""Configuration for the tunnel, read from the environment (systemd EnvironmentFile)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be a number, got {raw!r}") from exc


def default_state_dir() -> Path:
    """Where the tunnel records the link for the watcher on this machine."""
    override = _env("STATE_DIR")
    if override:
        return Path(override)
    system = Path("/var/lib/e4mc")
    if os.access(system.parent, os.W_OK):
        return system
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "e4mc"


@dataclass(frozen=True)
class TunnelConfig:
    mc_host: str
    mc_port: int
    broker_url: str
    relay_host: str | None
    relay_port: int | None
    state_dir: Path
    domain_suffix: str
    keepalive_interval: float
    idle_timeout: float
    reconnect_min: float
    reconnect_max: float
    heartbeat_interval: float
    log_level: str

    @classmethod
    def from_env(cls) -> "TunnelConfig":
        return cls(
            mc_host=_env("MC_HOST", "127.0.0.1") or "127.0.0.1",
            mc_port=_env_int("MC_PORT", 25565),
            broker_url=_env("BROKER_URL", "https://broker.e4mc.link/getBestRelay")
            or "https://broker.e4mc.link/getBestRelay",
            relay_host=_env("RELAY_HOST") or None,
            relay_port=_env_int("RELAY_PORT", 25575),
            state_dir=default_state_dir(),
            domain_suffix=_env("DOMAIN_SUFFIX", ".e4mc.link") or ".e4mc.link",
            keepalive_interval=max(1.0, _env_float("KEEPALIVE_INTERVAL", 15.0)),
            idle_timeout=max(5.0, _env_float("IDLE_TIMEOUT", 30.0)),
            reconnect_min=max(0.0, _env_float("RECONNECT_MIN", 1.0)),
            reconnect_max=max(1.0, _env_float("RECONNECT_MAX", 30.0)),
            heartbeat_interval=max(1.0, _env_float("HEARTBEAT_INTERVAL", 20.0)),
            log_level=(_env("LOG_LEVEL", "INFO") or "INFO").upper(),
        )
