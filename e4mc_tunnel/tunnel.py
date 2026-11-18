"""QUIClime relay client.

Wire behaviour copied from the e4mc mod and its relay:

* The client opens exactly one bidirectional QUIC stream, the control channel,
  and requests a domain assignment. The relay never opens more than one stream
  per client reachability-wise, and limits us to one ourselves.
* Every player that connects to the relay's TCP listener is matched by the
  hostname in their Minecraft handshake and handed to us on a *server-initiated*
  bidirectional stream as raw Minecraft bytes.
* We therefore only have to splice those streams into a TCP connection to the
  local Minecraft server, byte for byte, including the original handshake.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import ssl
import time
from datetime import datetime, timezone
from functools import partial

from aioquic.asyncio.client import connect
from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.quic import events
from aioquic.quic.configuration import QuicConfiguration

from . import broker, protocol
from .config import TunnelConfig
from .state import StateStore

LOG = logging.getLogger("e4mc.tunnel")

# Stop reading from the local server when this much data is queued for a
# player that is not draining it, instead of growing without bound.
_QUIC_PENDING_LIMIT = 8 * 1024 * 1024
_QUIC_PENDING_PAUSE = 0.02
_HALF_CLOSE_GRACE = 15.0
_SESSION_HEALTHY_AFTER = 60.0


def _sender_buffer(sender) -> bytearray | None:
    """Bytes written to a QUIC stream that the peer has not acknowledged yet.

    1.8.0 keeps them in ``QuicStreamSender._buffer`` (trimmed as ACKs arrive);
    the asyncio ``QuicStreamAdapter`` handed out as the stream's transport has
    no write-buffer size, so there is no public accessor. requirements.txt pins
    the version this field is read from, and ``_check_backpressure`` warns if an
    upgrade moves it.
    """
    buffer = getattr(sender, "buffer", None)
    if buffer is None:
        buffer = getattr(sender, "_buffer", None)
    return buffer if isinstance(buffer, bytearray) else None


class RelayProtocol(QuicConnectionProtocol):
    """One QUIC connection to the relay, including its player streams."""

    def __init__(self, quic, stream_handler=None, *, session: "TunnelSession") -> None:
        # Install our handler for peer-initiated streams. The control stream is
        # created explicitly in start() and never reaches this callback.
        super().__init__(quic, stream_handler=self._player_stream_opened)
        self._session = session
        self._control_buffer = bytearray()
        # Must outlive start(): letting the writer be collected closes the
        # stream, and the relay treats control-stream EOF as a dead tunnel.
        self._control_writer: asyncio.StreamWriter | None = None
        self._finished = asyncio.Event()
        self._player_tasks: set[asyncio.Task] = set()
        self._backpressure_warned = False

    # -- QUIC events ------------------------------------------------------

    def quic_event_received(self, event) -> None:
        super().quic_event_received(event)
        if isinstance(event, events.ConnectionTerminated):
            if event.error_code:
                LOG.info("relay closed the connection: %s %r", event.error_code, event.reason_phrase)
            else:
                LOG.info("relay closed the connection")
            self._finished.set()

    # -- control channel --------------------------------------------------

    async def start(self) -> None:
        reader, writer = await self.create_stream()
        self._control_writer = writer
        writer.write(protocol.encode_message({"kind": "probe_capabilities"}))
        writer.write(protocol.encode_message({"kind": "request_domain_assignment"}))
        await writer.drain()
        asyncio.create_task(self._read_control(reader), name="control-reader")

    async def _read_control(self, reader: asyncio.StreamReader) -> None:
        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    LOG.info("relay closed the control channel")
                    return
                self._control_buffer += chunk
                for message in protocol.iter_messages(self._control_buffer):
                    await self._handle_control(message)
        except (protocol.ProtocolError, ConnectionError) as exc:
            LOG.warning("control channel failed: %s", exc)
        finally:
            self._finished.set()

    async def _handle_control(self, message: dict) -> None:
        kind = message.get("kind")
        if kind == "domain_assignment_complete":
            domain = protocol.normalize_domain(
                message.get("domain"), self._session.config.domain_suffix
            )
            if domain is None:
                raise protocol.ProtocolError(
                    f"relay assigned an unusable domain: {message.get('domain')!r}"
                )
            await self._session.assign_domain(domain)
        elif kind == "has_capabilities":
            caps = message.get("caps") or []
            LOG.info("relay capabilities: %s", ", ".join(map(str, caps)) or "none")
        elif kind == "request_message_broadcast":
            LOG.info("relay broadcast: %s", message.get("message"))
        else:
            LOG.debug("ignoring control message %r", kind)

    async def run(self) -> None:
        await self.start()
        keepalive = asyncio.create_task(self._keepalive(), name="keepalive")
        try:
            await self._finished.wait()
        finally:
            keepalive.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await keepalive
            pending = list(self._player_tasks)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    async def _keepalive(self) -> None:
        interval = self._session.config.keepalive_interval
        while True:
            await asyncio.sleep(interval)
            try:
                await asyncio.wait_for(self.ping(), timeout=interval)
            except (asyncio.TimeoutError, ConnectionError):
                LOG.warning("relay stopped responding; dropping the tunnel")
                self._finished.set()
                self.close()
                return

    # -- player streams ---------------------------------------------------

    def _player_stream_opened(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(self._proxy_player(reader, writer), name="player")
        self._player_tasks.add(task)
        task.add_done_callback(self._player_tasks.discard)

    async def _proxy_player(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        config = self._session.config
        try:
            tcp_reader, tcp_writer = await asyncio.wait_for(
                asyncio.open_connection(config.mc_host, config.mc_port), timeout=10
            )
        except (OSError, asyncio.TimeoutError) as exc:
            LOG.warning(
                "cannot reach the Minecraft server at %s:%s: %s",
                config.mc_host,
                config.mc_port,
                exc,
            )
            writer.close()
            return

        LOG.info("player connected, proxying to %s:%s", config.mc_host, config.mc_port)
        self._check_backpressure(writer)
        up = asyncio.create_task(self._pump(reader, tcp_writer), name="player->mc")
        down = asyncio.create_task(
            self._pump(tcp_reader, writer, self._quic_pending(writer)), name="mc->player"
        )
        try:
            _, waiting = await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
            if waiting:
                _, waiting = await asyncio.wait(waiting, timeout=_HALF_CLOSE_GRACE)
            for task in waiting:
                task.cancel()
        finally:
            for task in (up, down):
                task.cancel()
            await asyncio.gather(up, down, return_exceptions=True)
            tcp_writer.close()
            try:
                await tcp_writer.wait_closed()
            except (OSError, ConnectionError):
                pass
            writer.close()
        LOG.info("player disconnected")

    async def _pump(self, source, target, pending=None) -> None:
        try:
            while True:
                data = await source.read(65536)
                if not data:
                    break
                target.write(data)
                await target.drain()
                if pending is not None:
                    while pending() > _QUIC_PENDING_LIMIT:
                        await asyncio.sleep(_QUIC_PENDING_PAUSE)
            target.write_eof()
        except (ConnectionError, OSError, RuntimeError):
            # Either side going away is normal; the other pump shuts down too.
            pass

    def _player_stream(self, writer: asyncio.StreamWriter):
        """The QUIC stream backing one player's writer, if it is still known."""
        streams = getattr(self._quic, "_streams", None)
        stream_id = writer.get_extra_info("stream_id")
        return streams.get(stream_id) if isinstance(streams, dict) else None

    def _check_backpressure(self, writer: asyncio.StreamWriter) -> None:
        """Warn once if this aioquic version no longer exposes the send buffer.

        ``_quic_pending`` reads aioquic's stream sender directly. If an upgrade
        moves that field, reads would silently return 0 and the memory cap would
        quietly stop working; say so instead.
        """
        if self._backpressure_warned:
            return
        sender = getattr(self._player_stream(writer), "sender", None)
        if _sender_buffer(sender) is not None:
            return
        self._backpressure_warned = True
        LOG.warning(
            "this aioquic version does not expose the stream send buffer; "
            "the %d MiB player backpressure limit is inactive",
            _QUIC_PENDING_LIMIT >> 20,
        )

    def _quic_pending(self, writer: asyncio.StreamWriter):
        """Bytes queued for the peer on this QUIC stream, or ``None`` for TCP."""
        if writer.get_extra_info("stream_id") is None:
            return None

        def pending() -> int:
            sender = getattr(self._player_stream(writer), "sender", None)
            buffer = _sender_buffer(sender)
            return len(buffer) if buffer is not None else 0

        return pending


class TunnelSession:
    """Supervises one relay connection and records the link it holds."""

    def __init__(self, config: TunnelConfig, state: StateStore) -> None:
        self.config = config
        self.state = state
        self.domain: str | None = None
        self.relay_id: str | None = None
        self._heartbeat_task: asyncio.Task | None = None

    async def assign_domain(self, domain: str) -> None:
        changed = domain != self.domain
        self.domain = domain
        self._record(connected=True)
        if changed:
            LOG.info("public link: %s", domain)
        if self._heartbeat_task is None:
            self._heartbeat_task = asyncio.create_task(self._heartbeat(), name="heartbeat")

    async def _heartbeat(self) -> None:
        """Keep state.json fresh so the local watcher can trust the link."""
        while True:
            await asyncio.sleep(self.config.heartbeat_interval)
            self._record(connected=True)

    async def run_once(self) -> None:
        relay = await asyncio.to_thread(broker.fetch_relay, self.config)
        self.relay_id = relay.id
        self._record(connected=False)

        configuration = QuicConfiguration(is_client=True)
        configuration.alpn_protocols = [protocol.ALPN]
        configuration.idle_timeout = self.config.idle_timeout
        configuration.max_data = 16 * 1024 * 1024
        configuration.max_stream_data = 2 * 1024 * 1024
        # The relay presents a certificate the mod never validates either; the
        # link is public and the payload is a plain TCP splice.
        configuration.verify_mode = ssl.CERT_NONE
        configuration.server_name = relay.host

        async with connect(
            relay.host,
            relay.port,
            configuration=configuration,
            create_protocol=partial(RelayProtocol, session=self),
        ) as connection:
            await connection.run()

    async def close(self) -> None:
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat_task
            self._heartbeat_task = None
        self._record(connected=False)

    # -- helpers ----------------------------------------------------------

    def _payload(self) -> dict:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return {
            "domain": self.domain,
            "relay": self.relay_id,
            "updated_at": now,
            "heartbeat_at": now,
            "version": 2,
        }

    def _record(self, *, connected: bool) -> None:
        payload = self._payload()
        payload["connected"] = connected
        self.state.write(payload)


async def supervise(config: TunnelConfig) -> None:
    """Keep a tunnel up forever, backing off between failures."""
    session = TunnelSession(config, StateStore(config.state_dir))

    delay = config.reconnect_min
    try:
        while True:
            started = time.monotonic()
            outcome = "closed"
            try:
                await session.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                outcome = str(exc) or exc.__class__.__name__
                LOG.warning("tunnel error: %s", outcome, exc_info=LOG.isEnabledFor(logging.DEBUG))

            lived = time.monotonic() - started
            await session.close()
            if lived >= _SESSION_HEALTHY_AFTER:
                delay = config.reconnect_min
            wait = min(delay, config.reconnect_max) + random.uniform(0.0, 1.0)
            LOG.info("tunnel %s after %.0fs; reconnecting in %.1fs", outcome, lived, wait)
            await asyncio.sleep(wait)
            # Grow from at most 1 s, never from reconnect_min: with
            # RECONNECT_MIN=0 the first retry is immediate, but every retry
            # after it still backs off instead of hammering the relay.
            delay = min(max(delay * 2, 1.0), config.reconnect_max)
    finally:
        await session.close()
