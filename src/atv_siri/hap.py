from __future__ import annotations

import asyncio
from typing import Any, Callable
from uuid import UUID

from pyhap.accessory import Accessory
from pyhap.accessory_driver import AccessoryDriver
from pyhap.characteristic import (
    Characteristic,
    HAP_FORMAT_BOOL,
    HAP_FORMAT_STRING,
    HAP_FORMAT_TLV8,
    HAP_FORMAT_UINT8,
    HAP_FORMAT_UINT32,
    PROP_FORMAT,
    PROP_MAX_VALUE,
    PROP_MIN_STEP,
    PROP_MIN_VALUE,
    PROP_PERMISSIONS,
    PROP_VALID_VALUES,
)
from pyhap.const import (
    CATEGORY_TARGET_CONTROLLER,
    HAP_PERMISSION_NOTIFY,
    HAP_PERMISSION_READ,
    HAP_PERMISSION_WRITE,
    HAP_PERMISSION_WRITE_RESPONSE,
)
from pyhap.hap_protocol import HAPServerProtocol
from pyhap.hap_server import HAPServer
from pyhap.service import Service

from .constants import (
    CHAR_ACTIVE,
    CHAR_ACTIVE_IDENTIFIER,
    CHAR_BUTTON_EVENT,
    CHAR_SELECTED_AUDIO_STREAM_CONFIGURATION,
    CHAR_SETUP_DATA_STREAM_TRANSPORT,
    CHAR_SIRI_INPUT_TYPE,
    CHAR_SUPPORTED_AUDIO_STREAM_CONFIGURATION,
    CHAR_SUPPORTED_DATA_STREAM_TRANSPORT_CONFIGURATION,
    CHAR_TARGET_CONTROL_LIST,
    CHAR_TARGET_CONTROL_SUPPORTED_CONFIGURATION,
    CHAR_VERSION,
    SERVICE_AUDIO_STREAM_MANAGEMENT,
    SERVICE_DATA_STREAM_TRANSPORT_MANAGEMENT,
    SERVICE_SIRI,
    SERVICE_TARGET_CONTROL,
    SERVICE_TARGET_CONTROL_MANAGEMENT,
)


class ContextCharacteristic(Characteristic):
    """Characteristic whose setter receives the originating HAP client address."""

    __slots__ = ("context_setter",)

    def __init__(self, *args: Any, context_setter: Callable[[Any, tuple[str, int] | None], Any] | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.context_setter = context_setter

    def client_update_value(self, value: Any, sender_client_addr: tuple[str, int] | None = None) -> Any:
        original_value = value
        if not self._always_null or original_value is not None:  # type: ignore[attr-defined]
            value = self.to_valid_value(value)
        if not self.allow_invalid_client_values:
            self.valid_value_or_raise(value)
        previous_value = self._value  # type: ignore[attr-defined]
        self.value = value
        if self.context_setter:
            response = self.context_setter(value, sender_client_addr)
        elif self.setter_callback:
            response = self.setter_callback(value)
        else:
            response = None
        if self._value != previous_value:  # type: ignore[attr-defined]
            self.notify(sender_client_addr)
        if self._always_null:  # type: ignore[attr-defined]
            self.value = None
        return response


class SiriHAPServerProtocol(HAPServerProtocol):
    """HAP protocol that retains the Pair Verify shared secret for HDS."""

    shared_secret: bytes | None = None

    def _process_response(self, response: Any) -> None:
        if response.shared_key:
            self.shared_secret = bytes(response.shared_key)
        super()._process_response(response)


class SiriHAPServer(HAPServer):
    async def async_start(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.server = await loop.create_server(
            lambda: SiriHAPServerProtocol(loop, self.connections, self.accessory_handler),
            self._addr_port[0],
            self._addr_port[1],
        )
        self.async_cleanup_connections()


class SiriAccessoryDriver(AccessoryDriver):
    """HAP-python driver with HDS session context and connection-lost callback."""

    def __init__(self, *args: Any, connection_lost_callback: Callable[[tuple[str, int]], None] | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        addr_port = self.http_server._addr_port  # HAP-python has no public accessor for this.
        self.http_server = SiriHAPServer(addr_port, self)
        self._external_connection_lost = connection_lost_callback

    def connection_lost(self, client: tuple[str, int]) -> None:
        super().connection_lost(client)
        if self._external_connection_lost:
            self._external_connection_lost(client)

    def protocol_for(self, client: tuple[str, int] | None) -> SiriHAPServerProtocol | None:
        if client is None:
            return None
        proto = self.http_server.connections.get(client)
        return proto if isinstance(proto, SiriHAPServerProtocol) else None

    def shared_secret_for(self, client: tuple[str, int] | None) -> bytes | None:
        proto = self.protocol_for(client)
        return proto.shared_secret if proto else None

    def is_admin(self, client: tuple[str, int] | None) -> bool:
        proto = self.protocol_for(client)
        if not proto or not proto.handler or not proto.handler.client_uuid:
            return False
        return self.state.is_admin(proto.handler.client_uuid)


def _char(
    name: str,
    uuid: str,
    fmt: str,
    perms: list[str],
    *,
    value: Any = None,
    getter: Callable[[], Any] | None = None,
    setter: Callable[[Any, tuple[str, int] | None], Any] | None = None,
    min_value: int | None = None,
    max_value: int | None = None,
    valid_values: list[int] | None = None,
) -> ContextCharacteristic:
    props: dict[str, Any] = {PROP_FORMAT: fmt, PROP_PERMISSIONS: perms}
    if min_value is not None:
        props[PROP_MIN_VALUE] = min_value
    if max_value is not None:
        props[PROP_MAX_VALUE] = max_value
    if min_value is not None or max_value is not None:
        props[PROP_MIN_STEP] = 1
    if valid_values is not None:
        props[PROP_VALID_VALUES] = {str(v): v for v in valid_values}
    char = ContextCharacteristic(name, UUID(uuid), props, context_setter=setter)
    if getter:
        char.getter_callback = getter
    if value is not None:
        char.set_value(value, should_notify=False)
    return char


def _service(name: str, uuid: str, *chars: Characteristic) -> Service:
    service = Service(UUID(uuid), name)
    service.add_characteristic(*chars)
    return service


class AppleTVRemoteAccessory(Accessory):
    category = CATEGORY_TARGET_CONTROLLER

    def __init__(self, driver: SiriAccessoryDriver, name: str, controller: Any) -> None:
        super().__init__(driver, name, aid=1)
        self.controller = controller
        self.set_info_service(
            manufacturer="atv-siri-py",
            model="Python Apple TV Siri Remote",
            serial_number=controller.config.username.replace(":", ""),
            firmware_revision=controller.version,
        )

        supported_target = _char(
            "TargetControlSupportedConfiguration",
            CHAR_TARGET_CONTROL_SUPPORTED_CONFIGURATION,
            HAP_FORMAT_TLV8,
            [HAP_PERMISSION_READ],
            getter=controller.target_supported_value,
        )
        self.supported_target_char = supported_target
        target_list = _char(
            "TargetControlList",
            CHAR_TARGET_CONTROL_LIST,
            HAP_FORMAT_TLV8,
            [HAP_PERMISSION_READ, HAP_PERMISSION_WRITE, HAP_PERMISSION_WRITE_RESPONSE],
            getter=controller.target_list_value,
            setter=controller.hap_target_list_write,
        )
        self.target_management_service = _service(
            "TargetControlManagement", SERVICE_TARGET_CONTROL_MANAGEMENT, supported_target, target_list
        )
        self.target_management_service.is_primary_service = True

        active_identifier = _char(
            "ActiveIdentifier",
            CHAR_ACTIVE_IDENTIFIER,
            HAP_FORMAT_UINT32,
            [HAP_PERMISSION_READ, HAP_PERMISSION_NOTIFY],
            getter=lambda: controller.active_identifier,
        )
        active = _char(
            "Active",
            CHAR_ACTIVE,
            HAP_FORMAT_UINT8,
            [HAP_PERMISSION_READ, HAP_PERMISSION_WRITE, HAP_PERMISSION_NOTIFY],
            getter=lambda: int(controller.active_client is not None),
            setter=controller.hap_active_write,
            min_value=0,
            max_value=1,
            valid_values=[0, 1],
        )
        button_event = _char(
            "ButtonEvent",
            CHAR_BUTTON_EVENT,
            HAP_FORMAT_TLV8,
            [HAP_PERMISSION_READ, HAP_PERMISSION_NOTIFY],
            getter=lambda: controller.last_button_event,
        )
        self.target_control_service = _service(
            "TargetControl", SERVICE_TARGET_CONTROL, active_identifier, active, button_event
        )
        self.active_identifier_char = active_identifier
        self.active_char = active
        self.button_event_char = button_event

        siri_input = _char("SiriInputType", CHAR_SIRI_INPUT_TYPE, HAP_FORMAT_UINT8, [HAP_PERMISSION_READ], value=0, min_value=0, max_value=0, valid_values=[0])
        self.siri_service = _service("Siri", SERVICE_SIRI, siri_input)

        supported_audio = _char(
            "SupportedAudioStreamConfiguration",
            CHAR_SUPPORTED_AUDIO_STREAM_CONFIGURATION,
            HAP_FORMAT_TLV8,
            [HAP_PERMISSION_READ],
            getter=controller.supported_audio_value,
        )
        selected_audio = _char(
            "SelectedAudioStreamConfiguration",
            CHAR_SELECTED_AUDIO_STREAM_CONFIGURATION,
            HAP_FORMAT_TLV8,
            [HAP_PERMISSION_READ, HAP_PERMISSION_WRITE],
            getter=controller.selected_audio_value,
            setter=controller.hap_selected_audio_write,
        )
        self.audio_service = _service("AudioStreamManagement", SERVICE_AUDIO_STREAM_MANAGEMENT, supported_audio, selected_audio)

        supported_hds = _char(
            "SupportedDataStreamTransportConfiguration",
            CHAR_SUPPORTED_DATA_STREAM_TRANSPORT_CONFIGURATION,
            HAP_FORMAT_TLV8,
            [HAP_PERMISSION_READ],
            getter=controller.supported_hds_value,
        )
        setup_hds = _char(
            "SetupDataStreamTransport",
            CHAR_SETUP_DATA_STREAM_TRANSPORT,
            HAP_FORMAT_TLV8,
            [HAP_PERMISSION_READ, HAP_PERMISSION_WRITE, HAP_PERMISSION_WRITE_RESPONSE],
            getter=controller.setup_hds_value,
            setter=controller.hap_setup_hds_write,
        )
        version = _char("Version", CHAR_VERSION, HAP_FORMAT_STRING, [HAP_PERMISSION_READ], value="1.0")
        self.hds_service = _service(
            "DataStreamTransportManagement", SERVICE_DATA_STREAM_TRANSPORT_MANAGEMENT, supported_hds, setup_hds, version
        )

        self.add_service(
            self.target_management_service,
            self.target_control_service,
            self.siri_service,
            self.audio_service,
            self.hds_service,
        )
        self.siri_service.add_linked_service(self.hds_service)
        self.siri_service.add_linked_service(self.audio_service)
