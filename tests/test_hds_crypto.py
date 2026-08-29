from atv_siri.hds import _hkdf, _nonce


def test_hds_nonce_layout():
    assert _nonce(0) == bytes(12)
    assert _nonce(1) == b"\x00" * 4 + b"\x01" + b"\x00" * 7


def test_hds_hkdf_direction_keys_differ():
    secret = bytes(range(32))
    salt = bytes(range(64))
    read = _hkdf(secret, salt, b"HDS-Read-Encryption-Key")
    write = _hkdf(secret, salt, b"HDS-Write-Encryption-Key")
    assert len(read) == 32
    assert len(write) == 32
    assert read != write
