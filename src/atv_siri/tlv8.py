from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable


def encode(*items: tuple[int, bytes | bytearray | str | int] | list) -> bytes:
    """Encode TLV8 entries.

    Accepts either ``encode((tag, value), ...)`` or ``encode([(tag, value), ...])``.
    Values longer than 255 bytes are split into repeated TLVs as required by HAP.
    """
    if len(items) == 1 and isinstance(items[0], list):
        entries = items[0]
    else:
        entries = items
    out = bytearray()
    for tag, raw in entries:
        if isinstance(raw, str):
            value = raw.encode()
        elif isinstance(raw, int):
            if not 0 <= raw <= 255:
                raise ValueError("integer TLV8 values must fit in one byte")
            value = bytes([raw])
        else:
            value = bytes(raw)
        if not value:
            out.extend((tag, 0))
            continue
        for start in range(0, len(value), 255):
            chunk = value[start : start + 255]
            out.extend((tag, len(chunk)))
            out.extend(chunk)
    return bytes(out)


def decode(data: bytes) -> dict[int, bytes]:
    """Decode a TLV8 buffer, concatenating adjacent repeated tags."""
    result: dict[int, bytearray] = {}
    i = 0
    last_tag: int | None = None
    while i < len(data):
        if i + 2 > len(data):
            raise ValueError("truncated TLV8 header")
        tag, length = data[i], data[i + 1]
        i += 2
        if i + length > len(data):
            raise ValueError("truncated TLV8 value")
        chunk = data[i : i + length]
        i += length
        if tag == last_tag and tag in result:
            result[tag].extend(chunk)
        elif tag in result:
            # Non-adjacent duplicate tags are uncommon for the control structures used
            # here. Preserve the most recent value rather than silently joining lists.
            result[tag] = bytearray(chunk)
        else:
            result[tag] = bytearray(chunk)
        last_tag = tag
    return {k: bytes(v) for k, v in result.items()}


def decode_list(data: bytes, separator_tag: int) -> list[dict[int, bytes]]:
    """Split a TLV8 stream whenever ``separator_tag`` starts a new object."""
    entries: list[list[tuple[int, bytes]]] = []
    current: list[tuple[int, bytes]] = []
    i = 0
    while i < len(data):
        tag, length = data[i], data[i + 1]
        i += 2
        chunk = data[i : i + length]
        i += length
        if tag == separator_tag and current:
            entries.append(current)
            current = []
        current.append((tag, chunk))
    if current:
        entries.append(current)
    return [decode(encode(entry)) for entry in entries]


def u16(value: int) -> bytes:
    return int(value).to_bytes(2, "little", signed=False)


def u32(value: int) -> bytes:
    return int(value).to_bytes(4, "little", signed=False)


def uint_le(value: int) -> bytes:
    if value < 0:
        raise ValueError("unsigned integer must be >= 0")
    width = max(1, (value.bit_length() + 7) // 8)
    return value.to_bytes(width, "little")


def read_u16(value: bytes) -> int:
    return int.from_bytes(value, "little", signed=False)


def read_u32(value: bytes) -> int:
    return int.from_bytes(value, "little", signed=False)
