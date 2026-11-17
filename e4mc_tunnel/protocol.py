"""QUIClime control-channel wire format.

Derived from vgskye/e4mc-quiclime (``src/proto.rs``, ``src/netty.rs``) and the
e4mc Minecraft mod (``QuiclimeSession.java``).

A control frame is ``varint(payload_length) || utf8(json)`` where the JSON
object carries a ``kind`` discriminator. The relay rejects frames larger than
8192 bytes and reads a varint length, so the length prefix must be encoded the
same way on both sides.

Messages used by the tunnel::

    ->  {"kind": "probe_capabilities"}
    ->  {"kind": "request_domain_assignment"}
    <-  {"kind": "has_capabilities", "caps": [...]}
    <-  {"kind": "domain_assignment_complete", "domain": "word-word.e4mc.link"}
    <-  {"kind": "request_message_broadcast", "message": "..."}

Player traffic does not use this framing: it arrives on server-initiated
bidirectional QUIC streams as raw Minecraft protocol bytes.
"""

from __future__ import annotations

import json
import re
from typing import Iterator

ALPN = "quiclime"
MAX_FRAME = 8192

_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class ProtocolError(Exception):
    """The relay sent something this client cannot interpret."""


def encode_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("varint cannot be negative")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def decode_varint(buffer: bytes | bytearray, offset: int = 0) -> tuple[int, int] | None:
    """Return ``(value, bytes_consumed)``, or ``None`` when the buffer is short."""
    value = 0
    for index in range(5):
        position = offset + index
        if position >= len(buffer):
            return None
        byte = buffer[position]
        value |= (byte & 0x7F) << (7 * index)
        if not byte & 0x80:
            return value, index + 1
    raise ProtocolError("varint exceeds five bytes")


def encode_message(message: dict) -> bytes:
    payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_FRAME:
        raise ProtocolError(f"control frame of {len(payload)} bytes is too large")
    return encode_varint(len(payload)) + payload


def iter_messages(buffer: bytearray) -> Iterator[dict]:
    """Yield every complete frame in ``buffer``, consuming those bytes."""
    while True:
        header = decode_varint(buffer)
        if header is None:
            return
        length, consumed = header
        if not 0 <= length <= MAX_FRAME:
            raise ProtocolError(f"control frame of {length} bytes is out of range")
        if len(buffer) < consumed + length:
            return
        payload = bytes(buffer[consumed : consumed + length])
        del buffer[: consumed + length]
        try:
            message = json.loads(payload)
        except ValueError as exc:
            raise ProtocolError("control frame was not valid JSON") from exc
        if not isinstance(message, dict) or "kind" not in message:
            raise ProtocolError("control frame has no kind")
        yield message


def normalize_domain(value: object, suffix: str) -> str | None:
    """Return a publishable hostname, or ``None`` if it is not one.

    Everything the tunnel forwards to the public link channel goes through
    here first, so a garbled or hostile relay response can never put an
    arbitrary URL on the GitHub Pages redirect.
    """
    if not isinstance(value, str):
        return None
    domain = value.strip().lower().rstrip(".")
    if not 1 <= len(domain) <= 253:
        return None
    if not suffix or not domain.endswith(suffix) or domain == suffix.lstrip("."):
        return None
    if any(not _LABEL.match(label) for label in domain.split(".")):
        return None
    return domain
