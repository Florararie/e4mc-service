"""``python -m e4mc_tunnel`` -- run the tunnel until SIGTERM/SIGINT."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from .config import TunnelConfig
from .tunnel import supervise


async def _run(config: TunnelConfig) -> None:
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signum, stop.set)

    task = asyncio.create_task(supervise(config), name="supervisor")
    await stop.wait()
    logging.getLogger("e4mc").info("shutting down")
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def main() -> int:
    config = TunnelConfig.from_env()
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        asyncio.run(_run(config))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
