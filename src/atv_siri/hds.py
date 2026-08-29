from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import os
import secrets
import struct
from typing import Any, Awaitable, Callable

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .constants import (
    HDS_PROTOCOL_CONTROL,
    HDS_PROTOCOL_TARGET_CONTROL,
    HDS_TOPIC_HELLO,
    HDS_TOPIC_WHOAMI,
    HDSStatus,
)
from .hds_codec import Int64, decode as hds_decode, encode as hds_encode

_LOGGER = logging.getLogger(__name__)
MAX_PAYLOAD_LENGTH = 0xFFFFF
PREPARED_SESSION_TIMEOUT = 10.0
MAX_PREPARED_SESSIONS = 16


def _hkdf(shared_secret: bytes, salt: bytes, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA512(), length=32, salt=salt, info=info).derive(shared_secret)


def _nonce(counter: int) -> bytes:
    if not 0 <= counter <= 0xFFFFFFFFFFFFFFFF:
        raise OverflowError("HDS nonce counter exhausted")
    return b"\x00" * 4 + struct.pack("<Q", counter)


@dataclass(slots=True)
class PreparedSession:
    client_addr: tuple[str, int]
    accessory_to_controller_key: bytes
    controller_to_accessory_key: bytes
    accessory_salt: bytes
    timeout: asyncio.TimerHandle | None = None


MessageHandler = Callable[
    ["HDSConnection", str, str, dict[str, Any], int | None],
    Awaitable[None] | None,
]


class HDSConnection:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        prepared: PreparedSession,
        first_payload: bytes,
        *,
        on_message: MessageHandler,
        on_close: Callable[["HDSConnection"], None],
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.prepared = prepared
        self.remote_address = writer.get_extra_info("peername")
        self._out_key = prepared.accessory_to_controller_key
        self._in_key = prepared.controller_to_accessory_key
        self._out_nonce = 0
        self._in_nonce = 1
        self._first_payload = first_payload
        self._on_message = on_message
        self._on_close = on_close
        self._responses: dict[int, asyncio.Future[tuple[int, dict[str, Any]]]] = {}
        self._closed = False
        self._send_lock = asyncio.Lock()
        self.target_identifier: int | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def closed(self) -> bool:
        return self._closed

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name=f"hds-{self.remote_address}")

    async def wait_closed(self) -> None:
        task = self._task
        if task and task is not asyncio.current_task():
            try:
                await task
            except asyncio.CancelledError:
                pass
        try:
            await self.writer.wait_closed()
        except (AttributeError, ConnectionError, OSError):
            pass

    async def _run(self) -> None:
        try:
            first = self._decode_message(self._first_payload)
            if not (first[0] == "request" and first[1] == HDS_PROTOCOL_CONTROL and first[2] == HDS_TOPIC_HELLO):
                raise ValueError("first HDS message was not control/hello request")
            await self.send_response(HDS_PROTOCOL_CONTROL, HDS_TOPIC_HELLO, first[4] or 0)
            while not self._closed:
                frame_header = await self.reader.readexactly(4)
                if frame_header[0] != 1:
                    raise ValueError(f"unsupported HDS frame type {frame_header[0]}")
                length = int.from_bytes(frame_header[1:4], "big")
                if length > MAX_PAYLOAD_LENGTH:
                    raise ValueError("HDS payload exceeds maximum size")
                encrypted = await self.reader.readexactly(length + 16)
                payload = ChaCha20Poly1305(self._in_key).decrypt(
                    _nonce(self._in_nonce), encrypted, frame_header
                )
                self._in_nonce += 1
                await self._dispatch(self._decode_message(payload))
        except asyncio.IncompleteReadError:
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.warning("HDS connection failed for %s", self.remote_address, exc_info=True)
        finally:
            self.close()

    def _decode_message(self, payload: bytes) -> tuple[str, str, str, dict[str, Any], int | None, int | None]:
        if not payload:
            raise ValueError("empty HDS payload")
        header_len = payload[0]
        if 1 + header_len > len(payload):
            raise ValueError("invalid HDS header length")
        header = hds_decode(payload[1 : 1 + header_len])
        message = hds_decode(payload[1 + header_len :])
        if not isinstance(header, dict) or not isinstance(message, dict):
            raise ValueError("HDS header/message must be dictionaries")
        protocol = str(header.get("protocol", ""))
        if not protocol:
            raise ValueError("HDS message is missing protocol")
        if "event" in header:
            return "event", protocol, str(header["event"]), message, None, None
        if "request" in header:
            if "id" not in header:
                raise ValueError("HDS request is missing id")
            return "request", protocol, str(header["request"]), message, int(header["id"]), None
        if "response" in header:
            if "id" not in header or "status" not in header:
                raise ValueError("HDS response is missing id/status")
            return "response", protocol, str(header["response"]), message, int(header["id"]), int(header["status"])
        raise ValueError(f"unknown HDS message header: {header!r}")

    async def _dispatch(self, decoded: tuple[str, str, str, dict[str, Any], int | None, int | None]) -> None:
        kind, protocol, topic, message, request_id, status = decoded
        if kind == "response":
            future = self._responses.pop(request_id or 0, None)
            if future and not future.done():
                future.set_result((status or 0, message))
            return
        result = self._on_message(self, protocol, topic, message, request_id)
        if asyncio.iscoroutine(result):
            await result

    async def _send(self, header: dict[str, Any], message: dict[str, Any]) -> None:
        async with self._send_lock:
            if self._closed:
                raise ConnectionError("HDS connection is closed")
            header_data = hds_encode(header)
            if len(header_data) > 255:
                raise ValueError("HDS encoded header exceeds one-byte header-length field")
            payload = bytes([len(header_data)]) + header_data + hds_encode(message)
            if len(payload) > MAX_PAYLOAD_LENGTH:
                raise ValueError("HDS payload too large")
            frame_header = bytes([1]) + len(payload).to_bytes(3, "big")
            encrypted = ChaCha20Poly1305(self._out_key).encrypt(
                _nonce(self._out_nonce), payload, frame_header
            )
            self._out_nonce += 1
            self.writer.write(frame_header + encrypted)
            await self.writer.drain()

    async def send_event(self, protocol: str, topic: str, message: dict[str, Any] | None = None) -> None:
        await self._send({"protocol": protocol, "event": topic}, message or {})

    async def send_response(
        self,
        protocol: str,
        topic: str,
        request_id: int,
        status: int = HDSStatus.SUCCESS,
        message: dict[str, Any] | None = None,
    ) -> None:
        await self._send(
            {
                "protocol": protocol,
                "response": topic,
                "id": Int64(request_id),
                "status": Int64(int(status)),
            },
            message or {},
        )

    async def send_request(
        self,
        protocol: str,
        topic: str,
        message: dict[str, Any] | None = None,
        *,
        timeout: float = 10.0,
    ) -> tuple[int, dict[str, Any]]:
        if timeout <= 0:
            raise ValueError("timeout must be > 0")
        loop = asyncio.get_running_loop()
        while True:
            request_id = secrets.randbits(32)
            if request_id not in self._responses:
                break
        future: asyncio.Future[tuple[int, dict[str, Any]]] = loop.create_future()
        self._responses[request_id] = future
        try:
            await self._send(
                {"protocol": protocol, "request": topic, "id": Int64(request_id)},
                message or {},
            )
            return await asyncio.wait_for(future, timeout)
        finally:
            self._responses.pop(request_id, None)
            if not future.done():
                future.cancel()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for future in self._responses.values():
            if not future.done():
                future.set_exception(ConnectionError("HDS connection closed"))
        self._responses.clear()
        try:
            self.writer.close()
        except Exception:
            pass
        try:
            self._on_close(self)
        except Exception:
            _LOGGER.exception("HDS close callback failed")


class HDSServer:
    """Accessory-side HomeKit Data Stream TCP server."""

    READ_INFO = b"HDS-Read-Encryption-Key"
    WRITE_INFO = b"HDS-Write-Encryption-Key"

    def __init__(
        self,
        *,
        host: str = "0.0.0.0",
        on_whoami: Callable[[int, HDSConnection], None] | None = None,
        on_event: Callable[[HDSConnection, str, str, dict[str, Any]], Awaitable[None] | None] | None = None,
        on_close: Callable[[HDSConnection], None] | None = None,
    ) -> None:
        self.host = host
        self.port: int | None = None
        self.server: asyncio.AbstractServer | None = None
        self.prepared: list[PreparedSession] = []
        self.connections: set[HDSConnection] = set()
        self.on_whoami = on_whoami
        self.on_event = on_event
        self.on_close = on_close

    async def start(self) -> None:
        if self.server:
            return
        self.server = await asyncio.start_server(self._accept, self.host, 0)
        sockets = self.server.sockets or []
        if not sockets:
            self.server.close()
            self.server = None
            raise RuntimeError("HDS TCP server did not expose a listening socket")
        self.port = int(sockets[0].getsockname()[1])
        _LOGGER.info("HDS server listening on %s:%d", self.host, self.port)

    async def stop(self) -> None:
        for prepared in list(self.prepared):
            self._remove_prepared(prepared)
        connections = list(self.connections)
        for connection in connections:
            connection.close()
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        if connections:
            await asyncio.gather(*(connection.wait_closed() for connection in connections), return_exceptions=True)
        self.connections.clear()
        self.port = None

    def prepare_session(
        self,
        client_addr: tuple[str, int],
        shared_secret: bytes,
        controller_salt: bytes,
    ) -> PreparedSession:
        if self.port is None:
            raise RuntimeError("HDS server is not started")
        if len(shared_secret) < 32:
            raise ValueError("HDS shared secret is unexpectedly short")
        if len(controller_salt) != 32:
            raise ValueError("controller HDS salt must be 32 bytes")

        for pending in list(self.prepared):
            if pending.client_addr == client_addr:
                self._remove_prepared(pending)
        if len(self.prepared) >= MAX_PREPARED_SESSIONS:
            raise RuntimeError("too many pending HDS sessions")

        accessory_salt = os.urandom(32)
        salt = controller_salt + accessory_salt
        session = PreparedSession(
            client_addr=client_addr,
            accessory_to_controller_key=_hkdf(shared_secret, salt, self.READ_INFO),
            controller_to_accessory_key=_hkdf(shared_secret, salt, self.WRITE_INFO),
            accessory_salt=accessory_salt,
        )
        loop = asyncio.get_running_loop()
        session.timeout = loop.call_later(PREPARED_SESSION_TIMEOUT, self._expire_prepared, session)
        self.prepared.append(session)
        return session

    def _remove_prepared(self, session: PreparedSession) -> None:
        if session.timeout:
            session.timeout.cancel()
            session.timeout = None
        try:
            self.prepared.remove(session)
        except ValueError:
            pass

    def _expire_prepared(self, session: PreparedSession) -> None:
        if session not in self.prepared:
            return
        self._remove_prepared(session)
        _LOGGER.debug("Prepared HDS session expired for %s", session.client_addr)

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            async with asyncio.timeout(PREPARED_SESSION_TIMEOUT):
                header = await reader.readexactly(4)
                if header[0] != 1:
                    raise ValueError("invalid initial HDS frame type")
                length = int.from_bytes(header[1:4], "big")
                if length > MAX_PAYLOAD_LENGTH:
                    raise ValueError("initial HDS payload exceeds maximum size")
                encrypted = await reader.readexactly(length + 16)

            found: PreparedSession | None = None
            first_payload: bytes | None = None
            for candidate in list(self.prepared):
                try:
                    first_payload = ChaCha20Poly1305(candidate.controller_to_accessory_key).decrypt(
                        _nonce(0), encrypted, header
                    )
                except Exception:
                    continue
                found = candidate
                break
            if not found or first_payload is None:
                raise ValueError("could not identify HDS session")
            self._remove_prepared(found)
            connection = HDSConnection(
                reader,
                writer,
                found,
                first_payload,
                on_message=self._handle_message,
                on_close=self._connection_closed,
            )
            self.connections.add(connection)
            connection.start()
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ValueError):
            _LOGGER.debug("Rejected incoming HDS connection", exc_info=True)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
        except Exception:
            _LOGGER.exception("Failed while accepting HDS connection")
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _handle_message(
        self,
        connection: HDSConnection,
        protocol: str,
        topic: str,
        message: dict[str, Any],
        request_id: int | None,
    ) -> None:
        if protocol == HDS_PROTOCOL_TARGET_CONTROL and topic == HDS_TOPIC_WHOAMI:
            if "identifier" not in message:
                raise ValueError("targetControl/whoami missing identifier")
            identifier = int(message["identifier"])
            connection.target_identifier = identifier
            if self.on_whoami:
                self.on_whoami(identifier, connection)
            return
        if self.on_event:
            result = self.on_event(connection, protocol, topic, message)
            if asyncio.iscoroutine(result):
                await result

    def close_for_client(self, client_addr: tuple[str, int]) -> None:
        """Close active and pending HDS sessions derived from a HAP session."""
        for pending in list(self.prepared):
            if pending.client_addr == client_addr:
                self._remove_prepared(pending)
        for connection in list(self.connections):
            if connection.prepared.client_addr == client_addr:
                connection.close()

    def close_all(self) -> None:
        for pending in list(self.prepared):
            self._remove_prepared(pending)
        for connection in list(self.connections):
            connection.close()

    def _connection_closed(self, connection: HDSConnection) -> None:
        self.connections.discard(connection)
        if self.on_close:
            self.on_close(connection)
