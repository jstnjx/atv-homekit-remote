"""HomeKit Data Stream (HDS) serialization codec.

This is a clean Python port of the data-format behavior used by HAP-NodeJS.
"""
from __future__ import annotations

from dataclasses import dataclass
import struct
import uuid as uuidlib
from typing import Any


@dataclass(frozen=True)
class Int8:
    value: int


@dataclass(frozen=True)
class Int16:
    value: int


@dataclass(frozen=True)
class Int32:
    value: int


@dataclass(frozen=True)
class Int64:
    value: int


@dataclass(frozen=True)
class Float32:
    value: float


@dataclass(frozen=True)
class Float64:
    value: float


@dataclass(frozen=True)
class HDSUUID:
    value: str


class Tag:
    TRUE = 0x01
    FALSE = 0x02
    TERMINATOR = 0x03
    NULL = 0x04
    UUID = 0x05
    DATE = 0x06
    INTEGER_MINUS_ONE = 0x07
    INTEGER_0 = 0x08
    INTEGER_39 = 0x2E
    INT8 = 0x30
    INT16 = 0x31
    INT32 = 0x32
    INT64 = 0x33
    FLOAT32 = 0x35
    FLOAT64 = 0x36
    UTF8_SHORT = 0x40
    UTF8_SHORT_END = 0x60
    UTF8_8 = 0x61
    UTF8_16 = 0x62
    UTF8_32 = 0x63
    UTF8_64 = 0x64
    UTF8_NULL = 0x6F
    DATA_SHORT = 0x70
    DATA_SHORT_END = 0x90
    DATA_8 = 0x91
    DATA_16 = 0x92
    DATA_32 = 0x93
    DATA_64 = 0x94
    DATA_TERMINATED = 0x9F
    COMPRESS = 0xA0
    COMPRESS_END = 0xCF
    ARRAY = 0xD0
    ARRAY_END = 0xDE
    ARRAY_TERMINATED = 0xDF
    DICT = 0xE0
    DICT_END = 0xEE
    DICT_TERMINATED = 0xEF


class Encoder:
    def __init__(self) -> None:
        self.buf = bytearray()
        self.seen: list[Any] = []

    def encode(self, value: Any) -> bytes:
        self._value(value)
        return bytes(self.buf)

    def _compress(self, value: Any) -> bool:
        try:
            index = self.seen.index(value)
        except ValueError:
            self.seen.append(value)
            return False
        if index <= Tag.COMPRESS_END - Tag.COMPRESS:
            self.buf.append(Tag.COMPRESS + index)
            return True
        return False

    def _value(self, value: Any) -> None:
        if value is None:
            self.buf.append(Tag.NULL)
        elif value is True:
            self.buf.append(Tag.TRUE)
        elif value is False:
            self.buf.append(Tag.FALSE)
        elif isinstance(value, Int8):
            self._wrapped(value, Tag.INT8, "<b")
        elif isinstance(value, Int16):
            self._wrapped(value, Tag.INT16, "<h")
        elif isinstance(value, Int32):
            self._wrapped(value, Tag.INT32, "<i")
        elif isinstance(value, Int64):
            self._wrapped(value, Tag.INT64, "<q")
        elif isinstance(value, Float32):
            self._wrapped(value, Tag.FLOAT32, "<f")
        elif isinstance(value, Float64):
            self._wrapped(value, Tag.FLOAT64, "<d")
        elif isinstance(value, HDSUUID):
            if not self._compress(value):
                self.buf.append(Tag.UUID)
                self.buf.extend(uuidlib.UUID(value.value).bytes)
        elif isinstance(value, int):
            self._integer(value)
        elif isinstance(value, float):
            self._wrapped(Float64(value), Tag.FLOAT64, "<d")
        elif isinstance(value, str):
            self._string(value)
        elif isinstance(value, (bytes, bytearray, memoryview)):
            self._bytes(bytes(value))
        elif isinstance(value, (list, tuple)):
            self._array(value)
        elif isinstance(value, dict):
            self._dict(value)
        else:
            raise TypeError(f"unsupported HDS value: {type(value)!r}")

    def _wrapped(self, wrapper: Any, tag: int, fmt: str) -> None:
        if self._compress(wrapper):
            return
        self.buf.append(tag)
        self.buf.extend(struct.pack(fmt, wrapper.value))

    def _integer(self, value: int) -> None:
        if value == -1:
            self.buf.append(Tag.INTEGER_MINUS_ONE)
        elif 0 <= value <= 39:
            self.buf.append(Tag.INTEGER_0 + value)
        elif -128 <= value <= 127:
            self._wrapped(Int8(value), Tag.INT8, "<b")
        elif -32768 <= value <= 32767:
            self._wrapped(Int16(value), Tag.INT16, "<h")
        elif -(2**31) <= value <= (2**31 - 1):
            self._wrapped(Int32(value), Tag.INT32, "<i")
        elif -(2**63) <= value <= (2**63 - 1):
            self._wrapped(Int64(value), Tag.INT64, "<q")
        else:
            raise OverflowError("integer does not fit HDS int64")

    def _length(self, length: int, tags: tuple[int, int, int, int]) -> None:
        if length <= 0xFF:
            self.buf.extend((tags[0], length))
        elif length <= 0xFFFF:
            self.buf.append(tags[1]); self.buf.extend(struct.pack("<H", length))
        elif length <= 0xFFFFFFFF:
            self.buf.append(tags[2]); self.buf.extend(struct.pack("<I", length))
        else:
            self.buf.append(tags[3]); self.buf.extend(struct.pack("<Q", length))

    def _string(self, value: str) -> None:
        if self._compress(value):
            return
        raw = value.encode("utf-8")
        if len(raw) <= 32:
            self.buf.append(Tag.UTF8_SHORT + len(raw)); self.buf.extend(raw)
        else:
            self._length(len(raw), (Tag.UTF8_8, Tag.UTF8_16, Tag.UTF8_32, Tag.UTF8_64)); self.buf.extend(raw)

    def _bytes(self, value: bytes) -> None:
        if self._compress(value):
            return
        if len(value) <= 32:
            self.buf.append(Tag.DATA_SHORT + len(value)); self.buf.extend(value)
        else:
            self._length(len(value), (Tag.DATA_8, Tag.DATA_16, Tag.DATA_32, Tag.DATA_64)); self.buf.extend(value)

    def _array(self, value: list | tuple) -> None:
        if len(value) <= 12:
            self.buf.append(Tag.ARRAY + len(value))
            for item in value: self._value(item)
        else:
            self.buf.append(Tag.ARRAY_TERMINATED)
            for item in value: self._value(item)
            self.buf.append(Tag.TERMINATOR)

    def _dict(self, value: dict) -> None:
        entries = list(value.items())
        if len(entries) <= 14:
            self.buf.append(Tag.DICT + len(entries))
            for key, item in entries: self._value(key); self._value(item)
        else:
            self.buf.append(Tag.DICT_TERMINATED)
            for key, item in entries: self._value(key); self._value(item)
            self.buf.append(Tag.TERMINATOR)


class _Terminator:
    pass


TERMINATOR = _Terminator()


class Decoder:
    def __init__(self, data: bytes) -> None:
        self.data = memoryview(data)
        self.i = 0
        self.seen: list[Any] = []

    def decode(self) -> Any:
        value = self._value()
        if self.i != len(self.data):
            raise ValueError(f"trailing HDS bytes: {len(self.data) - self.i}")
        return value

    def _take(self, count: int) -> bytes:
        if self.i + count > len(self.data):
            raise ValueError("truncated HDS payload")
        value = bytes(self.data[self.i : self.i + count]); self.i += count
        return value

    def _track(self, value: Any) -> Any:
        self.seen.append(value); return value

    def _tag(self) -> int:
        return self._take(1)[0]

    def _unpack(self, fmt: str) -> Any:
        return struct.unpack(fmt, self._take(struct.calcsize(fmt)))[0]

    def _length(self, width: int) -> int:
        return int.from_bytes(self._take(width), "little")

    def _value(self) -> Any:
        tag = self._tag()
        if tag == Tag.TRUE: return self._track(True)
        if tag == Tag.FALSE: return self._track(False)
        if tag == Tag.TERMINATOR: return TERMINATOR
        if tag == Tag.NULL: return None
        if tag == Tag.UUID: return self._track(str(uuidlib.UUID(bytes=self._take(16))))
        if tag == Tag.DATE: return self._track(self._unpack("<d"))
        if tag == Tag.INTEGER_MINUS_ONE: return self._track(-1)
        if Tag.INTEGER_0 <= tag <= Tag.INTEGER_39: return self._track(tag - Tag.INTEGER_0)
        if tag == Tag.INT8: return self._track(self._unpack("<b"))
        if tag == Tag.INT16: return self._track(self._unpack("<h"))
        if tag == Tag.INT32: return self._track(self._unpack("<i"))
        if tag == Tag.INT64: return self._track(self._unpack("<q"))
        if tag == Tag.FLOAT32: return self._track(self._unpack("<f"))
        if tag == Tag.FLOAT64: return self._track(self._unpack("<d"))
        if Tag.UTF8_SHORT <= tag <= Tag.UTF8_SHORT_END: return self._track(self._take(tag - Tag.UTF8_SHORT).decode())
        if tag in (Tag.UTF8_8, Tag.UTF8_16, Tag.UTF8_32, Tag.UTF8_64):
            width = {Tag.UTF8_8:1, Tag.UTF8_16:2, Tag.UTF8_32:4, Tag.UTF8_64:8}[tag]
            return self._track(self._take(self._length(width)).decode())
        if Tag.DATA_SHORT <= tag <= Tag.DATA_SHORT_END: return self._track(self._take(tag - Tag.DATA_SHORT))
        if tag in (Tag.DATA_8, Tag.DATA_16, Tag.DATA_32, Tag.DATA_64):
            width = {Tag.DATA_8:1, Tag.DATA_16:2, Tag.DATA_32:4, Tag.DATA_64:8}[tag]
            return self._track(self._take(self._length(width)))
        if Tag.COMPRESS <= tag <= Tag.COMPRESS_END:
            index = tag - Tag.COMPRESS
            if index >= len(self.seen): raise ValueError("invalid HDS compression reference")
            return self.seen[index]
        if Tag.ARRAY <= tag <= Tag.ARRAY_END:
            return [self._value() for _ in range(tag - Tag.ARRAY)]
        if tag == Tag.ARRAY_TERMINATED:
            values = []
            while True:
                item = self._value()
                if item is TERMINATOR: return values
                values.append(item)
        if Tag.DICT <= tag <= Tag.DICT_END:
            result = {}
            for _ in range(tag - Tag.DICT):
                key = self._value()
                result[key] = self._value()
            return result
        if tag == Tag.DICT_TERMINATED:
            result = {}
            while True:
                key = self._value()
                if key is TERMINATOR: return result
                result[key] = self._value()
        raise ValueError(f"unknown HDS tag 0x{tag:02x}")


def encode(value: Any) -> bytes:
    return Encoder().encode(value)


def decode(data: bytes) -> Any:
    return Decoder(data).decode()
