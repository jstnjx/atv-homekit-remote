import base64
import tempfile

from atv_homekit_remote.constants import AUDIO_CODEC_OPUS, HDS_TRANSFER_TRANSPORT_CONFIGURATION
from atv_homekit_remote.remote import AppleTVHomeKitRemote, RemoteConfig
from atv_homekit_remote.tlv8 import decode


def make_remote():
    temp = tempfile.TemporaryDirectory()
    remote = AppleTVHomeKitRemote(RemoteConfig(state_dir=temp.name))
    remote._test_temp_dir = temp
    return remote


def test_audio_configuration_is_opus():
    remote = make_remote()
    outer = decode(base64.b64decode(remote.supported_audio_value()))
    codec = decode(outer[1])
    assert codec[1][0] == AUDIO_CODEC_OPUS


def test_supported_hds_configuration():
    remote = make_remote()
    outer = decode(base64.b64decode(remote.supported_hds_value()))
    assert HDS_TRANSFER_TRANSPORT_CONFIGURATION in outer
    inner = decode(outer[HDS_TRANSFER_TRANSPORT_CONFIGURATION])
    assert inner[1] == b"\x00"


def test_target_supported_configuration_includes_hardware_flag():
    remote = make_remote()
    supported = decode(base64.b64decode(remote.target_supported_value()))
    assert supported[4] == b"\x01"
