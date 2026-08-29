from __future__ import annotations

import asyncio
from dataclasses import dataclass
import math
import struct
from typing import TYPE_CHECKING

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
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 320
FRAME_BYTES = FRAME_SAMPLES * 2  # signed 16-bit mono


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
            raise RuntimeError("opuslib-next is required for Siri audio encoding") from exc
        self._encoder = opuslib_next.Encoder(SAMPLE_RATE, CHANNELS, "voip")

    def encode(self, pcm: bytes) -> AudioFrame:
        if len(pcm) != FRAME_BYTES:
            raise ValueError(f"PCM frame must be exactly {FRAME_BYTES} bytes")
        encoded = self._encoder.encode(pcm, FRAME_SAMPLES)
        # HAP-NodeJS reports normalized RMS in [0, 1].
        square_sum = 0.0
        for (sample,) in struct.iter_unpack("<h", pcm):
            normalized = sample / 32768.0
            square_sum += normalized * normalized
        rms = math.sqrt(square_sum / FRAME_SAMPLES)
        return AudioFrame(bytes(encoded), rms)


class SiriSession:
    """One Siri utterance over an already-associated Apple TV HDS connection."""

    def __init__(self, connection: "HDSConnection", *, target_identifier: int) -> None:
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

    def start(self) -> None:
        if self._open_task is not None:
            return
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
            self.stream_id = int(message["streamId"])
            await self._flush(force=self._ending)
        except BaseException as exc:
            self._error = exc
            self._closed = True
            self._done.set()
            raise

    async def write(self, pcm: bytes) -> None:
        if self._ending or self._closed:
            raise RuntimeError("Siri session is already ending/closed")
        self._pcm_buffer.extend(pcm)
        while len(self._pcm_buffer) >= FRAME_BYTES:
            frame = bytes(self._pcm_buffer[:FRAME_BYTES])
            del self._pcm_buffer[:FRAME_BYTES]
            self._frames.append(self.encoder.encode(frame))
            if self.stream_id is not None:
                await self._flush(force=False)

    async def _flush(self, *, force: bool) -> None:
        if self.stream_id is None:
            return
        sent_eos = False
        while len(self._frames) >= 5 or (force and self._frames):
            count = min(5, len(self._frames))
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
            await self.connection.send_event(
                HDS_PROTOCOL_DATA_SEND,
                HDS_TOPIC_DATA,
                {
                    "packets": packets,
                    "streamId": Int64(self.stream_id),
                    "endOfStream": bool(force and not self._frames),
                },
            )
            if force and not self._frames:
                sent_eos = True
                break
        if force and not self._frames and not sent_eos:
            # The protocol needs a final endOfStream marker even when there were no
            # queued packets (matching HAP-NodeJS popSome() behavior on stop).
            await self.connection.send_event(
                HDS_PROTOCOL_DATA_SEND,
                HDS_TOPIC_DATA,
                {"packets": [], "streamId": Int64(self.stream_id), "endOfStream": True},
            )

    async def finish(self, *, timeout: float = 10.0, pad_final_frame: bool = True) -> None:
        if self._closed:
            if self._error:
                raise self._error
            return
        if self._ending:
            await self.wait_closed(timeout=timeout)
            return
        self._ending = True
        if self._pcm_buffer:
            if pad_final_frame:
                self._pcm_buffer.extend(b"\x00" * (FRAME_BYTES - len(self._pcm_buffer)))
                self._frames.append(self.encoder.encode(bytes(self._pcm_buffer)))
            self._pcm_buffer.clear()
        if self._open_task:
            await self._open_task
        await self._flush(force=True)
        try:
            await self.wait_closed(timeout=timeout)
        except TimeoutError:
            if self.stream_id is not None and not self.connection.closed:
                await self.connection.send_event(
                    HDS_PROTOCOL_DATA_SEND,
                    HDS_TOPIC_CLOSE,
                    {"streamId": Int64(self.stream_id), "reason": Int64(HDSCloseReason.TIMEOUT)},
                )
            self._closed = True
            self._done.set()
            raise

    async def wait_closed(self, *, timeout: float = 10.0) -> None:
        await asyncio.wait_for(self._done.wait(), timeout)
        if self._error:
            raise self._error

    async def handle_ack(self, end_of_stream: bool) -> None:
        if self._closed or not end_of_stream or self.stream_id is None:
            return
        await self.connection.send_event(
            HDS_PROTOCOL_DATA_SEND,
            HDS_TOPIC_CLOSE,
            {"streamId": Int64(self.stream_id), "reason": Int64(HDSCloseReason.NORMAL)},
        )
        self._closed = True
        self._done.set()

    def handle_close(self, reason: int) -> None:
        if self._closed:
            return
        if reason != HDSCloseReason.NORMAL:
            self._error = RuntimeError(f"Apple TV closed Siri stream with reason {reason}")
        self._closed = True
        self._done.set()

    async def cancel(self, reason: HDSCloseReason = HDSCloseReason.CANCELLED) -> None:
        if self.stream_id is not None and not self.connection.closed:
            await self.connection.send_event(
                HDS_PROTOCOL_DATA_SEND,
                HDS_TOPIC_CLOSE,
                {"streamId": Int64(self.stream_id), "reason": Int64(reason)},
            )
        self._closed = True
        self._done.set()
