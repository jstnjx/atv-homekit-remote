from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

TLV_MAX_CHUNK = 255


@dataclass(frozen=True, slots=True)
class TLVSegment:
    tag: int
    value: bytes


def _normalize_value(raw: bytes | bytearray | memoryview | str | int) -> bytes:
    if isinstance(raw, str):
        return raw.encode("utf-8")
    if isinstance(raw, int):
        if not 0 <= raw <= 0xFF:
            raise ValueError("integer TLV8 values must fit in one byte")
        return bytes((raw,))
    return bytes(raw)


def encode(*items: tuple[int, bytes | bytearray | memoryview | str | int] | Sequence[tuple[int, bytes | bytearray | memoryview | str | int]]) -> bytes:
    """Encode HomeKit TLV8 entries, splitting values longer than 255 bytes."""
    if len(items) == 1 and isinstance(items[0], Sequence) and not isinstance(items[0], tuple):
        entries = items[0]
    else:
        entries = items

    out = bytearray()
    for entry in entries:
        if not isinstance(entry, tuple) or len(entry) != 2:
            raise TypeError("TLV8 entries must be (tag, value) tuples")
        tag, raw = entry
        if not 0 <= int(tag) <= 0xFF:
            raise ValueError("TLV8 tag must fit in one byte")
        value = _normalize_value(raw)
        if not value:
            out.extend((int(tag), 0))
            continue
        for start in range(0, len(value), TLV_MAX_CHUNK):
            chunk = value[start : start + TLV_MAX_CHUNK]
            out.extend((int(tag), len(chunk)))
            out.extend(chunk)
    return bytes(out)


def segments(data: bytes | bytearray | memoryview) -> list[TLVSegment]:
    """Parse raw TLV8 segments without collapsing repeated tags."""
    raw = bytes(data)
    result: list[TLVSegment] = []
    index = 0
    while index < len(raw):
        if index + 2 > len(raw):
            raise ValueError("truncated TLV8 header")
        tag = raw[index]
        length = raw[index + 1]
        index += 2
        end = index + length
        if end > len(raw):
            raise ValueError("truncated TLV8 value")
        result.append(TLVSegment(tag, raw[index:end]))
        index = end
    return result


def decode(data: bytes | bytearray | memoryview) -> dict[int, bytes]:
    """Decode TLV8 into a mapping, respecting HAP's 255-byte continuation rule."""
    result: dict[int, bytes] = {}
    previous: TLVSegment | None = None
    for segment in segments(data):
        if (
            previous is not None
            and segment.tag == previous.tag
            and len(previous.value) == TLV_MAX_CHUNK
            and segment.tag in result
        ):
            result[segment.tag] += segment.value
        else:
            result[segment.tag] = segment.value
        previous = segment
    return result


def decode_list(data: bytes | bytearray | memoryview, separator_tag: int) -> list[dict[int, bytes]]:
    """Decode a TLV8 list whose items begin with ``separator_tag``."""
    parsed = segments(data)
    groups: list[list[TLVSegment]] = []
    current: list[TLVSegment] = []
    previous: TLVSegment | None = None

    for segment in parsed:
        starts_item = (
            segment.tag == separator_tag
            and current
            and not (
                previous is not None
                and previous.tag == separator_tag
                and len(previous.value) == TLV_MAX_CHUNK
            )
        )
        if starts_item:
            groups.append(current)
            current = []
        current.append(segment)
        previous = segment
    if current:
        groups.append(current)

    result: list[dict[int, bytes]] = []
    for group in groups:
        encoded = bytearray()
        for segment in group:
            encoded.extend((segment.tag, len(segment.value)))
            encoded.extend(segment.value)
        result.append(decode(encoded))
    return result


def u16(value: int) -> bytes:
    return int(value).to_bytes(2, "little", signed=False)


def u32(value: int) -> bytes:
    return int(value).to_bytes(4, "little", signed=False)


def uint_le(value: int) -> bytes:
    if value < 0:
        raise ValueError("unsigned integer must be >= 0")
    width = max(1, (int(value).bit_length() + 7) // 8)
    return int(value).to_bytes(width, "little")


def read_u16(value: bytes) -> int:
    if len(value) != 2:
        raise ValueError("uint16 TLV value must be exactly 2 bytes")
    return int.from_bytes(value, "little", signed=False)


def read_u32(value: bytes) -> int:
    if len(value) != 4:
        raise ValueError("uint32 TLV value must be exactly 4 bytes")
    return int.from_bytes(value, "little", signed=False)
