from atv_homekit_remote.audio import FRAME_BYTES, OpusEncoder


def test_opus_encoder_generates_frame():
    encoder = OpusEncoder()
    frame = encoder.encode(bytes(FRAME_BYTES))
    assert frame.data
    assert frame.rms == 0.0
