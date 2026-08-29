import math

from atv_siri.hds_codec import Float32, Int64, decode, encode


def test_hds_nested_roundtrip():
    value = {
        "protocol": "dataSend",
        "packets": [
            {
                "data": b"opus",
                "metadata": {"rms": Float32(0.25), "sequenceNumber": Int64(7)},
            }
        ],
        "streamId": Int64(99),
        "endOfStream": False,
    }
    result = decode(encode(value))
    assert result["protocol"] == "dataSend"
    assert result["packets"][0]["data"] == b"opus"
    assert math.isclose(result["packets"][0]["metadata"]["rms"], 0.25)
    assert result["packets"][0]["metadata"]["sequenceNumber"] == 7
    assert result["streamId"] == 99
    assert result["endOfStream"] is False


def test_hds_compression_is_decodable():
    # Repeated strings/data should be eligible for compression references.
    value = ["same", "same", b"same", b"same", 42, 42]
    result = decode(encode(value))
    assert result == value
