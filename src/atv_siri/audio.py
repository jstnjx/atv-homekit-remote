from __future__ import annotations

import asyncio
from dataclasses import dataclass
import math
import struct
from typing import Callable, TYPE_CHECKING

from .constants import (
    HDSCloseReason,
    HDS_PROTOCOL_DATA_SEND,
    HDS_TOPIC_CLOSE,
    HDS_TOPIC_DATA,
    HDS_TOPIC_OPEN,
)
from .hds_codec import Float32, Int64

if TYPE_CHECKING:
    from .hds import HDSConnection

SAMPLE_RATE = 16_000
CHANNELS = 1
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
FRAME_BYTES = FRAME_SAMPLES * 2
FRAMES_PER_HDS_EVENT = 5


@dataclass(slots=True)
class AudioFrame:
    data: bytes
    rms: float


class OpusEncoder:
    """16 kHz mono PCM16 -> Opus encoder for Apple TV Siri input."""

    def __init__(self) -> None:
        try:
            import opuslib_next
        except ImportError as exc:  # pragma: no cover - dependency error path
            raise RuntimeError("opuslib-next-bundled is required for Siri audio encoding") from exc
        self._encoder = opuslib_next.Encoder(SAMPLE_RATE, CHANNELS, "voip")

    def encode(self, pcm: bytes) -> AudioFrame:
        if len(pcm) != FRAME_BYTES:
            raise ValueError(f"PCM frame must be exactly {FRAME_BYTES} bytes")
        encoded = self._encoder.encode(pcm, FRAME_SAMPLES)
        square_sum = 0.0
        for (sample,) in struct.iter_unpack("<h", pcm):
            normalized = sample / 32768.0
            square_sum += normalized * normalized
        rms = math.sqrt(square_sum / FRAME_SAMPLES)
        return AudioFrame(bytes(encoded), rms)


class SiriSession:
    """One Siri utterance over an associated Apple TV HDS connection."""

    def __init__(
        self,
        connection: "HDSConnection",
        *,
        target_identifier: int,
        on_open: Callable[["SiriSession"], None] | None = None,
    ) -> None:
        self.connection = connection
        self.target_identifier = target_identifier
        self.encoder = OpusEncoder()
        self.stream_id: int | None = None
        self.sequence = 0
        self._pcm_buffer = bytearray()
        self._frames: list[AudioFrame] = []
        self._open_task: asyncio.Task[None] | None = None
        self._done = asyncio.Event()
        self._error: BaseException | None = None
        self._ending = False
        self._closed = False
        self._eos_sent = False
        self._close_sent = False
        self._cancel_reason: HDSCloseReason | None = None
        self._io_lock = asyncio.Lock()
        self._on_open = on_open

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def ending(self) -> bool:
        return self._ending

    def start(self) -> None:
        if self._open_task is None:
            self._open_task = asyncio.create_task(self._open(), name="siri-hds-open")

    async def _open(self) -> None:
        try:
            status, message = await self.connection.send_request(
                HDS_PROTOCOL_DATA_SEND,
                HDS_TOPIC_OPEN,
                {"target": "controller", "type": "audio.siri"},
            )
            if status:
                raise RuntimeError(f"Apple TV rejected Siri dataSend/open with HDS status {status}")
            if "streamId" not in message:
                raise RuntimeError("Apple TV dataSend/open response did not include streamId")
            self.stream_id = int(message["streamId"])
            if self.stream_id < 0:
                raise RuntimeError("Apple TV returned an invalid Siri streamId")

            if self._on_open:
                self._on_open(self)

            if self._cancel_reason is not None:
                await self._send_close(self._cancel_reason)
                self._mark_done()
                return
            async with self._io_lock:
                await self._flush_locked(force=self._ending)
        except asyncio.CancelledError:
            self._error = asyncio.CancelledError()
            self._mark_done()
            raise
        except BaseException as exc:
            self._error = exc
            self._mark_done()
            raise

    async def write(self, pcm: bytes | bytearray | memoryview) -> None:
        raw = bytes(pcm)
        if not raw:
            return
        async with self._io_lock:
            if self._ending or self._closed:
                raise RuntimeError("Siri session is already ending/closed")
            self._pcm_buffer.extend(raw)
            while len(self._pcm_buffer) >= FRAME_BYTES:
                frame = bytes(self._pcm_buffer[:FRAME_BYTES])
                del self._pcm_buffer[:FRAME_BYTES]
                self._frames.append(self.encoder.encode(frame))
            if self.stream_id is not None:
                await self._flush_locked(force=False)

    async def _flush_locked(self, *, force: bool) -> None:
        if self.stream_id is None or self._closed:
            return
        if force and self._eos_sent:
            return

        while len(self._frames) >= FRAMES_PER_HDS_EVENT or (force and self._frames):
            count = min(FRAMES_PER_HDS_EVENT, len(self._frames))
            frames = self._frames[:count]
            del self._frames[:count]
            packets = []
            for frame in frames:
                packets.append(
                    {
                        "data": frame.data,
                        "metadata": {
                            "rms": Float32(frame.rms),
                            "sequenceNumber": Int64(self.sequence),
                        },
                    }
                )
                self.sequence += 1
            is_eos = bool(force and not self._frames)
            await self.connection.send_event(
                HDS_PROTOCOL_DATA_SEND,
                HDS_TOPIC_DATA,
                {
                    "packets": packets,
                    "streamId": Int64(self.stream_id),
                    "endOfStream": is_eos,
                },
            )
            if is_eos:
                self._eos_sent = True
                return

        if force and not self._frames and not self._eos_sent:
            await self.connection.send_event(
                HDS_PROTOCOL_DATA_SEND,
                HDS_TOPIC_DATA,
                {"packets": [], "streamId": Int64(self.stream_id), "endOfStream": True},
            )
            self._eos_sent = True

    async def finish(self, *, timeout: float = 10.0, pad_final_frame: bool = True) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be > 0")
        if self._closed:
            if self._error:
                raise self._error
            return

        async with self._io_lock:
            if not self._ending:
                self._ending = True
                if self._pcm_buffer:
                    if pad_final_frame:
                        self._pcm_buffer.extend(b"\x00" * (FRAME_BYTES - len(self._pcm_buffer)))
                        self._frames.append(self.encoder.encode(bytes(self._pcm_buffer)))
                    self._pcm_buffer.clear()

        if self._open_task:
            await self._open_task
        if self._closed:
            if self._error:
                raise self._error
            return

        async with self._io_lock:
            await self._flush_locked(force=True)

        try:
            await self.wait_closed(timeout=timeout)
        except TimeoutError:
            try:
                await self._send_close(HDSCloseReason.TIMEOUT)
            finally:
                self._error = TimeoutError("timed out waiting for Siri end-of-stream acknowledgement")
                self._mark_done()
            raise self._error

    async def wait_closed(self, *, timeout: float | None = 10.0) -> None:
        if timeout is None:
            await self._done.wait()
        else:
            await asyncio.wait_for(self._done.wait(), timeout)
        if self._error:
            raise self._error

    async def handle_ack(self, end_of_stream: bool) -> None:
        if self._closed or not end_of_stream or self.stream_id is None:
            return
        try:
            await self._send_close(HDSCloseReason.NORMAL)
        except BaseException as exc:
            self._error = exc
            raise
        finally:
            self._mark_done()

    def handle_close(self, reason: int | HDSCloseReason) -> None:
        if self._closed:
            return
        try:
            close_reason = HDSCloseReason(int(reason))
        except ValueError:
            close_reason = HDSCloseReason.UNEXPECTED_FAILURE
        if close_reason != HDSCloseReason.NORMAL:
            self._error = RuntimeError(f"Apple TV closed Siri stream with reason {int(reason)}")
        self._mark_done()

    async def cancel(
        self,
        reason: HDSCloseReason = HDSCloseReason.CANCELLED,
        *,
        open_wait_timeout: float = 2.0,
    ) -> None:
        if self._closed:
            return
        self._ending = True
        self._cancel_reason = reason

        if self._open_task and not self._open_task.done() and self.stream_id is None:
            try:
                await asyncio.wait_for(asyncio.shield(self._open_task), open_wait_timeout)
            except TimeoutError:
                return
            except BaseException:
                return

        if self.stream_id is not None:
            try:
                await self._send_close(reason)
            finally:
                self._mark_done()
        else:
            self._mark_done()

    async def _send_close(self, reason: HDSCloseReason) -> None:
        if self._close_sent or self.stream_id is None or self.connection.closed:
            return
        await self.connection.send_event(
            HDS_PROTOCOL_DATA_SEND,
            HDS_TOPIC_CLOSE,
            {"streamId": Int64(self.stream_id), "reason": Int64(int(reason))},
        )
        self._close_sent = True

    def _mark_done(self) -> None:
        self._closed = True
        self._done.set()
