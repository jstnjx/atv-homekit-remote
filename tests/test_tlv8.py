import base64

from atv_homekit_remote.tlv8 import decode, encode, read_u16, read_u32, u16, u32


def test_tlv_roundtrip_and_chunking():
    raw = b"x" * 600
    encoded = encode((1, raw), (2, u16(65530)), (3, u32(0xDEADBEEF)))
    decoded = decode(encoded)
    assert decoded[1] == raw
    assert read_u16(decoded[2]) == 65530
    assert read_u32(decoded[3]) == 0xDEADBEEF


def test_hds_supported_transport_known_value():
    # outer tag 1 wraps inner transport-type tag 1 = HomeKit Data Stream (0)
    encoded = encode((1, encode((1, 0))))
    assert base64.b64encode(encoded).decode() == "AQMBAQA="
