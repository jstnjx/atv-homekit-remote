from __future__ import annotations

import asyncio
import base64
import binascii
from dataclasses import asdict, dataclass, field
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any, AsyncIterable, Iterable

from . import constants as C
from .audio import FRAME_BYTES, FRAME_MS, SiriSession
from .hap import AppleTVRemoteAccessory, SiriAccessoryDriver
from .hds import HDSConnection, HDSServer
from .tlv8 import decode as tlv_decode
from .tlv8 import decode_list, encode as tlv_encode, read_u16, read_u32, u16, u32, uint_le
from .version import __version__

_LOGGER = logging.getLogger(__name__)
_MAX_TARGETS = 10
_MAX_TARGET_NAME_BYTES = 255
_USERNAME_RE = re.compile(r"^(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
_PIN_RE = re.compile(r"^\d{3}-\d{2}-\d{3}$")


@dataclass(slots=True)
class RemoteConfig:
    """Runtime configuration for :class:`AppleTVHomeKitRemote`.

    ``username`` and ``pincode`` deliberately default to ``None``. HAP-python will
    generate a unique HomeKit accessory identity and PIN and persist them in
    ``state_dir``. Supplying explicit values is useful for controlled deployments.
    """

    name: str = "Apple TV HomeKit Remote"
    username: str | None = None
    pincode: str | None = None
    port: int = 47129
    listen_address: str | None = None
    advertised_address: str | None = None
    hds_listen_address: str = "0.0.0.0"
    state_dir: str = ".atv-homekit-remote"

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("name must not be empty")
        if not 1 <= int(self.port) <= 65535:
            raise ValueError("port must be in range 1..65535")
        if self.username is not None:
            if not _USERNAME_RE.fullmatch(self.username):
                raise ValueError("username must be a HomeKit MAC-style identifier (AA:BB:CC:DD:EE:FF)")
            self.username = self.username.upper()
        if self.pincode is not None and not _PIN_RE.fullmatch(self.pincode):
            raise ValueError("pincode must have the form 123-45-678")
        if not self.state_dir:
            raise ValueError("state_dir must not be empty")


@dataclass(slots=True)
class ButtonConfiguration:
    button_id: int
    button_type: int
    button_name: str | None = None


@dataclass(slots=True)
class TargetConfiguration:
    target_identifier: int
    target_name: str | None = None
    target_category: int | None = None
    buttons: dict[int, ButtonConfiguration] = field(default_factory=dict)


class AppleTVHomeKitRemote:
    """Pure-Python HomeKit Target Controller with Apple TV Siri audio support."""

    version = __version__

    def __init__(self, config: RemoteConfig | None = None) -> None:
        self.config = config or RemoteConfig()
        self.state_dir = Path(self.config.state_dir).expanduser().resolve()
        self._ensure_state_dir()
        self.targets_path = self.state_dir / "targets.json"

        self.targets: dict[int, TargetConfiguration] = {}
        self.active_identifier = 0
        self.active_client: tuple[str, int] | None = None
        self.last_button_event = ""
        self._last_hds_setup = ""
        self._selected_audio = self._build_audio_configuration(selected=True)
        self._siri_advertised = True

        self.hds_connections: dict[int, HDSConnection] = {}
        self.siri_sessions: dict[int, SiriSession] = {}
        self._active_siri_session: SiriSession | None = None
        self._siri_watch_task: asyncio.Task[None] | None = None
        self._siri_lock = asyncio.Lock()
        self._started = False
        self._load_targets()

        self.driver: SiriAccessoryDriver | None = None
        self.accessory: AppleTVRemoteAccessory | None = None
        self.hds = HDSServer(
            host=self.config.hds_listen_address,
            on_whoami=self._hds_whoami,
            on_event=self._hds_event,
            on_close=self._hds_connection_closed,
        )

    @property
    def started(self) -> bool:
        return self._started

    @property
    def pincode(self) -> str | None:
        if self.driver is not None:
            return self.driver.state.pincode.decode("ascii")
        return self.config.pincode

    @property
    def username(self) -> str | None:
        if self.driver is not None:
            return self.driver.state.mac
        return self.config.username

    async def start(self) -> None:
        if self._started:
            return
        self._ensure_state_dir()
        if self.driver is None:
            loop = asyncio.get_running_loop()
            persist_file = str(self.state_dir / "hap.state")
            kwargs: dict[str, Any] = {
                "port": self.config.port,
                "persist_file": persist_file,
                "connection_lost_callback": self._hap_connection_lost,
                "loop": loop,
            }
            if self.config.advertised_address is not None:
                kwargs["address"] = self.config.advertised_address
                kwargs["advertised_address"] = self.config.advertised_address
            if self.config.listen_address is not None:
                kwargs["listen_address"] = self.config.listen_address
            if self.config.pincode is not None:
                kwargs["pincode"] = bytearray(self.config.pincode.encode("ascii"))
            if self.config.username is not None:
                kwargs["mac"] = self.config.username
            self.driver = SiriAccessoryDriver(**kwargs)
            self.accessory = AppleTVRemoteAccessory(self.driver, self.config.name, self)
            self.driver.add_accessory(self.accessory)

        hds_started = False
        try:
            await self.hds.start()
            hds_started = True
            await self.driver.async_start()
        except BaseException:
            if hds_started:
                await self.hds.stop()
            raise
        self._started = True
        _LOGGER.info(
            "Apple TV HomeKit remote started as %s; HomeKit pairing code: %s",
            self.username,
            self.pincode,
        )

    async def stop(self) -> None:
        if not self._started and self.hds.server is None:
            return
        session = self._active_siri_session
        if session is not None and not session.closed:
            try:
                await session.cancel(open_wait_timeout=2.0)
            except BaseException:
                _LOGGER.debug("Could not cancel active Siri session during shutdown", exc_info=True)
        await self.hds.stop()
        if self.driver is not None and self._started:
            try:
                await self.driver.async_stop()
            except BaseException:
                _LOGGER.exception("Failed to stop HAP driver cleanly")
                raise
            finally:
                self._started = False
        else:
            self._started = False
        watch = self._siri_watch_task
        if watch is not None and not watch.done():
            try:
                await asyncio.wait_for(asyncio.shield(watch), 2.5)
            except (TimeoutError, asyncio.CancelledError):
                watch.cancel()
        self._siri_watch_task = None
        self._active_siri_session = None
        self.siri_sessions.clear()
        self.hds_connections.clear()
        self.active_client = None

    async def __aenter__(self) -> "AppleTVHomeKitRemote":
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.stop()

    def _hap_objects(self) -> tuple[SiriAccessoryDriver, AppleTVRemoteAccessory]:
        if self.driver is None or self.accessory is None:
            raise RuntimeError("remote is not started; call await remote.start() first")
        return self.driver, self.accessory

    @property
    def state(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "name": self.config.name,
            "username": self.username,
            "active_identifier": self.active_identifier,
            "active": self.active_client is not None,
            "configured_targets": [
                self._target_to_dict(target)
                for target in sorted(self.targets.values(), key=lambda item: item.target_identifier)
            ],
            "hds_targets": sorted(identifier for identifier, conn in self.hds_connections.items() if not conn.closed),
            "siri_ready": self.siri_ready,
            "siri_in_progress": bool(self._active_siri_session and not self._active_siri_session.closed),
        }

    @property
    def siri_ready(self) -> bool:
        connection = self.hds_connections.get(self.active_identifier)
        return bool(self.active_client and connection and not connection.closed and self._siri_advertised)

    def set_active_identifier(self, target_identifier: int) -> None:
        target_identifier = int(target_identifier)
        if not 0 <= target_identifier <= 0xFFFFFFFF:
            raise ValueError("target identifier must fit uint32")
        if target_identifier != 0 and target_identifier not in self.targets:
            raise ValueError(f"unknown target identifier {target_identifier}")
        if target_identifier == self.active_identifier:
            return
        old_identifier = self.active_identifier
        self.active_identifier = target_identifier
        self.active_client = None
        if self.accessory is not None:
            self.accessory.active_identifier_char.set_value(target_identifier)
            self.accessory.active_char.set_value(0)
        self._save_targets()
        session = self._active_siri_session
        if session is not None and not session.closed and session.target_identifier == old_identifier:
            self._schedule_session_cancel(session)

    async def press(
        self,
        button: C.Button | str | int,
        *,
        hold_ms: int = 200,
        target: int | None = None,
    ) -> None:
        if isinstance(button, str):
            try:
                parsed_button = C.Button[button.upper()]
            except KeyError as exc:
                raise ValueError(f"unknown button {button!r}") from exc
        else:
            try:
                parsed_button = C.Button(button)
            except ValueError as exc:
                raise ValueError(f"unknown button {button!r}") from exc
        if parsed_button in {C.Button.UNDEFINED, C.Button.SIRI}:
            raise ValueError("SIRI carries audio; use start_siri()/send_pcm()")
        if not 0 <= int(hold_ms) <= 60_000:
            raise ValueError("hold_ms must be in range 0..60000")
        if target is not None and int(target) != self.active_identifier:
            self.set_active_identifier(int(target))
        self._send_button(parsed_button, C.ButtonState.DOWN)
        await asyncio.sleep(int(hold_ms) / 1000)
        self._send_button(parsed_button, C.ButtonState.UP)

    async def start_siri(self, *, target: int | None = None, wait_timeout: float = 4.0) -> SiriSession:
        if wait_timeout <= 0:
            raise ValueError("wait_timeout must be > 0")
        try:
            await asyncio.wait_for(self._siri_lock.acquire(), wait_timeout)
        except TimeoutError as exc:
            raise RuntimeError("another Siri utterance is already in progress") from exc
        session: SiriSession | None = None
        try:
            if target is not None and int(target) != self.active_identifier:
                self.set_active_identifier(int(target))
            identifier = self.active_identifier
            if identifier == 0:
                raise RuntimeError("no Apple TV target is selected")
            if self.active_client is None:
                raise RuntimeError("selected Apple TV has not marked the Target Control service active")
            connection = self.hds_connections.get(identifier)
            if not connection or connection.closed:
                raise RuntimeError("selected Apple TV has no active HomeKit Data Stream connection")
            if not self._siri_advertised:
                raise RuntimeError("Siri capability is temporarily disabled")
            session = SiriSession(connection, target_identifier=identifier, on_open=self._siri_session_opened)
            self._active_siri_session = session
            session.start()
            self._siri_watch_task = asyncio.create_task(
                self._watch_siri_session(session),
                name=f"siri-session-{identifier}",
            )
            return session
        except BaseException:
            if session is self._active_siri_session:
                self._active_siri_session = None
            if self._siri_lock.locked():
                self._siri_lock.release()
            raise

    async def send_pcm(
        self,
        pcm: bytes | bytearray | memoryview | AsyncIterable[bytes] | Iterable[bytes],
        *,
        target: int | None = None,
        realtime: bool = True,
    ) -> None:
        """Send one Siri utterance as signed 16-bit, 16 kHz, mono PCM."""
        session = await self.start_siri(target=target)
        try:
            if isinstance(pcm, (bytes, bytearray, memoryview)):
                raw = bytes(pcm)
                loop = asyncio.get_running_loop()
                next_deadline = loop.time()
                for offset in range(0, len(raw), FRAME_BYTES):
                    chunk = raw[offset : offset + FRAME_BYTES]
                    await session.write(chunk)
                    if realtime and offset + FRAME_BYTES < len(raw):
                        next_deadline += FRAME_MS / 1000
                        delay = next_deadline - loop.time()
                        if delay > 0:
                            await asyncio.sleep(delay)
            elif hasattr(pcm, "__aiter__"):
                async for chunk in pcm:  # type: ignore[union-attr]
                    await session.write(chunk)
            else:
                for chunk in pcm:  # type: ignore[union-attr]
                    await session.write(chunk)
            await session.finish()
        except BaseException:
            cancel_task = asyncio.create_task(session.cancel(), name="cancel-siri-after-send-error")
            try:
                await asyncio.shield(cancel_task)
            except BaseException:
                pass
            raise

    async def recover_hds(self, *, phase_delay: float = 3.0) -> None:
        if phase_delay < 0:
            raise ValueError("phase_delay must be >= 0")
        driver, accessory = self._hap_objects()
        session = self._active_siri_session
        if session is not None and not session.closed:
            await session.cancel()
        self.hds.close_all()
        self.hds_connections.clear()
        self._siri_advertised = False
        accessory.supported_target_char.set_value(self.target_supported_value())
        driver.config_changed()
        if phase_delay:
            await asyncio.sleep(phase_delay)
        self._siri_advertised = True
        accessory.supported_target_char.set_value(self.target_supported_value())
        driver.config_changed()

    def target_supported_value(self) -> str:
        buttons = [
            C.Button.MENU, C.Button.PLAY_PAUSE, C.Button.TV_HOME, C.Button.SELECT,
            C.Button.ARROW_UP, C.Button.ARROW_RIGHT, C.Button.ARROW_DOWN, C.Button.ARROW_LEFT,
            C.Button.VOLUME_UP, C.Button.VOLUME_DOWN, C.Button.POWER, C.Button.GENERIC,
        ]
        if self._siri_advertised:
            buttons.append(C.Button.SIRI)
        button_data = b"".join(
            tlv_encode(
                (C.SUPPORTED_BUTTON_ID, 100 + int(button)),
                (C.SUPPORTED_BUTTON_TYPE, u16(int(button))),
            )
            for button in buttons
        )
        value = b"".join([
            tlv_encode((C.TC_MAXIMUM_TARGETS, _MAX_TARGETS)),
            tlv_encode((C.TC_TICKS_PER_SECOND, uint_le(1000))),
            tlv_encode((C.TC_SUPPORTED_BUTTON_CONFIGURATION, button_data)),
            tlv_encode((C.TC_TYPE, 1 if self._siri_advertised else 0)),
        ])
        return base64.b64encode(value).decode("ascii")

    def target_list_value(self) -> str:
        encoded = bytearray()
        for target in sorted(self.targets.values(), key=lambda item: item.target_identifier):
            parts = [tlv_encode((C.TARGET_IDENTIFIER, u32(target.target_identifier)))]
            if target.target_name is not None:
                parts.append(tlv_encode((C.TARGET_NAME, target.target_name)))
            if target.target_category is not None:
                parts.append(tlv_encode((C.TARGET_CATEGORY, u16(target.target_category))))
            if target.buttons:
                button_data = bytearray()
                for button in sorted(target.buttons.values(), key=lambda item: item.button_id):
                    one = bytearray(tlv_encode(
                        (C.BUTTON_CONFIGURATION_ID, button.button_id),
                        (C.BUTTON_CONFIGURATION_TYPE, u16(button.button_type)),
                    ))
                    if button.button_name:
                        one.extend(tlv_encode((C.BUTTON_CONFIGURATION_NAME, button.button_name)))
                    button_data.extend(one)
                parts.append(tlv_encode((C.TARGET_BUTTON_CONFIGURATION, bytes(button_data))))
            encoded.extend(tlv_encode((C.TC_LIST_TARGET_CONFIGURATION, b"".join(parts))))
        return base64.b64encode(bytes(encoded)).decode("ascii")

    def hap_target_list_write(self, value: str, client: tuple[str, int] | None) -> str:
        driver, accessory = self._hap_objects()
        if client is None or not driver.is_admin(client):
            raise PermissionError("TargetControlList writes require an authenticated HomeKit administrator")
        data = tlv_decode(self._decode_b64(value, "TargetControlList"))
        operation_raw = self._required(data, C.TC_LIST_OPERATION, "TargetControlList operation")
        if len(operation_raw) != 1:
            raise ValueError("TargetControlList operation must be one byte")
        try:
            operation = C.TargetOperation(operation_raw[0])
        except ValueError as exc:
            raise ValueError(f"unsupported target operation {operation_raw[0]}") from exc
        config = self._parse_target(data.get(C.TC_LIST_TARGET_CONFIGURATION))
        if operation == C.TargetOperation.ADD:
            if config is None:
                raise ValueError("ADD requires target configuration")
            if config.target_identifier not in self.targets and len(self.targets) >= _MAX_TARGETS:
                raise ValueError(f"maximum of {_MAX_TARGETS} Apple TV targets reached")
            self.targets[config.target_identifier] = config
            if self.active_identifier == 0:
                self.active_identifier = config.target_identifier
                accessory.active_identifier_char.set_value(self.active_identifier)
        elif operation == C.TargetOperation.UPDATE:
            if config is None or config.target_identifier not in self.targets:
                raise ValueError("UPDATE references unknown target")
            current = self.targets[config.target_identifier]
            if config.target_name is not None:
                current.target_name = config.target_name
            if config.target_category is not None:
                current.target_category = config.target_category
            if config.buttons:
                current.buttons.update(config.buttons)
        elif operation == C.TargetOperation.REMOVE:
            if config is None or config.target_identifier not in self.targets:
                raise ValueError("REMOVE references unknown target")
            current = self.targets[config.target_identifier]
            if config.buttons:
                for button_id in config.buttons:
                    current.buttons.pop(button_id, None)
            else:
                del self.targets[config.target_identifier]
                connection = self.hds_connections.pop(config.target_identifier, None)
                if connection:
                    connection.close()
                if self.active_identifier == config.target_identifier:
                    self.set_active_identifier(next(iter(sorted(self.targets)), 0))
        elif operation == C.TargetOperation.RESET:
            if config is not None:
                raise ValueError("RESET must not include target configuration")
            self.targets.clear()
            self.hds.close_all()
            self.hds_connections.clear()
            self.active_identifier = 0
            self.active_client = None
            accessory.active_identifier_char.set_value(0)
            accessory.active_char.set_value(0)
            self._schedule_session_cancel(self._active_siri_session)
        elif operation == C.TargetOperation.LIST:
            if config is not None:
                raise ValueError("LIST must not include target configuration")
        else:
            raise ValueError(f"unsupported target operation {operation}")
        self._save_targets()
        return self.target_list_value()

    def hap_active_identifier_write(self, value: Any, client: tuple[str, int] | None) -> None:
        if client is None:
            raise ValueError("ActiveIdentifier write is missing HAP connection context")
        self.set_active_identifier(int(value))

    def hap_active_write(self, value: Any, client: tuple[str, int] | None) -> None:
        if self.active_identifier == 0:
            raise ValueError("no target selected")
        if client is None:
            raise ValueError("Active write is missing HAP connection context")
        if bool(value):
            self.active_client = client
            return
        if self.active_client is not None and client != self.active_client:
            raise PermissionError("only the active HomeKit session can deactivate Target Control")
        self.active_client = None
        self._schedule_session_cancel(self._active_siri_session)

    def supported_audio_value(self) -> str:
        return self._build_audio_configuration(selected=False)

    def selected_audio_value(self) -> str:
        return self._selected_audio

    def hap_selected_audio_write(self, value: str, client: tuple[str, int] | None) -> None:
        if client is None:
            raise ValueError("audio configuration write is missing HAP connection context")
        outer = tlv_decode(self._decode_b64(value, "SelectedAudioStreamConfiguration"))
        codec = tlv_decode(self._required(outer, C.AUDIO_STREAM_CONFIGURATION, "audio stream configuration"))
        parameters = tlv_decode(self._required(codec, C.AUDIO_CODEC_PARAMETERS, "audio codec parameters"))
        codec_type = self._one_byte(codec, C.AUDIO_CODEC_TYPE, "audio codec type")
        channels = self._one_byte(parameters, C.AUDIO_CHANNELS, "audio channels")
        bitrate = self._one_byte(parameters, C.AUDIO_BITRATE, "audio bitrate")
        sample_rate = self._one_byte(parameters, C.AUDIO_SAMPLE_RATE, "audio sample rate")
        packet_time = parameters.get(C.AUDIO_PACKET_TIME)
        if codec_type != C.AUDIO_CODEC_OPUS:
            raise ValueError("only Opus Siri input is supported")
        if channels != 1 or sample_rate != C.AUDIO_SAMPLE_RATE_16_KHZ:
            raise ValueError("only 16 kHz mono Siri input is supported")
        if bitrate != C.AUDIO_BITRATE_VARIABLE:
            raise ValueError("only variable-bitrate Opus Siri input is supported")
        if packet_time is not None and (len(packet_time) != 1 or packet_time[0] != FRAME_MS):
            raise ValueError(f"only {FRAME_MS} ms Opus packets are supported")
        self._selected_audio = value

    def supported_hds_value(self) -> str:
        value = tlv_encode((
            C.HDS_TRANSFER_TRANSPORT_CONFIGURATION,
            tlv_encode((C.HDS_TRANSPORT_TYPE, C.HDS_HOMEKIT_DATA_STREAM)),
        ))
        return base64.b64encode(value).decode("ascii")

    def setup_hds_value(self) -> str:
        return self._last_hds_setup

    def hap_setup_hds_write(self, value: str, client: tuple[str, int] | None) -> str:
        if client is None:
            raise ValueError("HDS setup needs HAP connection context")
        data = tlv_decode(self._decode_b64(value, "SetupDataStreamTransport"))
        command = self._one_byte(data, C.HDS_SESSION_COMMAND_TYPE, "HDS session command")
        transport = self._one_byte(data, C.HDS_SESSION_TRANSPORT_TYPE, "HDS transport type")
        if command != C.HDS_START_SESSION:
            raise ValueError("unsupported HDS session command")
        if transport != C.HDS_HOMEKIT_DATA_STREAM:
            raise ValueError("unsupported HDS transport")
        controller_salt = self._required(data, C.HDS_CONTROLLER_KEY_SALT, "controller HDS salt")
        if len(controller_salt) != 32:
            raise ValueError("invalid controller HDS salt")
        driver, _ = self._hap_objects()
        shared_secret = driver.shared_secret_for(client)
        if not shared_secret:
            raise RuntimeError("Pair Verify shared secret unavailable for HDS setup")
        prepared = self.hds.prepare_session(client, shared_secret, controller_salt)
        if self.hds.port is None:
            raise RuntimeError("HDS server is not listening")
        session_params = tlv_encode((C.HDS_TCP_LISTENING_PORT, u16(self.hds.port)))
        stripped = b"".join([
            tlv_encode((C.HDS_SETUP_STATUS, 0)),
            tlv_encode((C.HDS_SETUP_SESSION_PARAMETERS, session_params)),
        ])
        self._last_hds_setup = base64.b64encode(stripped).decode("ascii")
        response = stripped + tlv_encode((C.HDS_ACCESSORY_KEY_SALT, prepared.accessory_salt))
        return base64.b64encode(response).decode("ascii")

    def _send_button(self, button: C.Button, state: C.ButtonState) -> None:
        if self.active_identifier == 0:
            raise RuntimeError("no target selected")
        if self.active_client is None:
            raise RuntimeError("selected target is not active")
        button_id = 100 + int(button)
        timestamp = int(time.time() * 1000)
        event = b"".join([
            tlv_encode((C.BUTTON_EVENT_ID, button_id)),
            tlv_encode((C.BUTTON_EVENT_STATE, int(state))),
            tlv_encode((C.BUTTON_EVENT_TIMESTAMP, timestamp.to_bytes(8, "little", signed=False))),
            tlv_encode((C.BUTTON_EVENT_ACTIVE_IDENTIFIER, u32(self.active_identifier))),
        ])
        self.last_button_event = base64.b64encode(event).decode("ascii")
        _, accessory = self._hap_objects()
        accessory.button_event_char.set_value(self.last_button_event)

    def _hds_whoami(self, identifier: int, connection: HDSConnection) -> None:
        if identifier <= 0 or identifier not in self.targets:
            _LOGGER.warning("Rejecting HDS whoami for unknown target identifier %d", identifier)
            connection.close()
            return
        old = self.hds_connections.get(identifier)
        if old and old is not connection and not old.closed:
            old.close()
        self.hds_connections[identifier] = connection
        _LOGGER.info("Apple TV target %d opened HDS", identifier)

    async def _hds_event(self, connection: HDSConnection, protocol: str, topic: str, message: dict[str, Any]) -> None:
        if protocol != C.HDS_PROTOCOL_DATA_SEND or topic not in {C.HDS_TOPIC_ACK, C.HDS_TOPIC_CLOSE}:
            return
        try:
            stream_id = int(message["streamId"])
        except (KeyError, TypeError, ValueError):
            _LOGGER.warning("Ignoring malformed dataSend/%s without a valid streamId", topic)
            return
        session = self.siri_sessions.get(stream_id)
        if session is None or session.connection is not connection:
            _LOGGER.debug("Ignoring dataSend/%s for unknown or mismatched stream %d", topic, stream_id)
            return
        if topic == C.HDS_TOPIC_ACK:
            await session.handle_ack(bool(message.get("endOfStream", False)))
        else:
            session.handle_close(int(message.get("reason", C.HDSCloseReason.UNEXPECTED_FAILURE)))

    def _siri_session_opened(self, session: SiriSession) -> None:
        if session.stream_id is None:
            raise RuntimeError("cannot register Siri session before streamId is assigned")
        existing = self.siri_sessions.get(session.stream_id)
        if existing is not None and existing is not session:
            raise RuntimeError(f"Apple TV reused active Siri streamId {session.stream_id}")
        self.siri_sessions[session.stream_id] = session

    async def _watch_siri_session(self, session: SiriSession) -> None:
        try:
            await session.wait_closed(timeout=None)
        except asyncio.CancelledError:
            raise
        except BaseException:
            _LOGGER.debug("Siri session ended with an error", exc_info=True)
        finally:
            if session.stream_id is not None and self.siri_sessions.get(session.stream_id) is session:
                self.siri_sessions.pop(session.stream_id, None)
            if self._active_siri_session is session:
                self._active_siri_session = None
                if self._siri_lock.locked():
                    self._siri_lock.release()
            if self._siri_watch_task is asyncio.current_task():
                self._siri_watch_task = None

    def _hds_connection_closed(self, connection: HDSConnection) -> None:
        if connection.target_identifier is not None:
            current = self.hds_connections.get(connection.target_identifier)
            if current is connection:
                self.hds_connections.pop(connection.target_identifier, None)
        session = self._active_siri_session
        if session is not None and session.connection is connection and not session.closed:
            session.handle_close(C.HDSCloseReason.UNEXPECTED_FAILURE)
        for stream_id, mapped in list(self.siri_sessions.items()):
            if mapped.connection is connection:
                if not mapped.closed:
                    mapped.handle_close(C.HDSCloseReason.UNEXPECTED_FAILURE)
                self.siri_sessions.pop(stream_id, None)

    def _hap_connection_lost(self, client: tuple[str, int]) -> None:
        self.hds.close_for_client(client)
        if client == self.active_client:
            self.active_client = None
            self._schedule_session_cancel(self._active_siri_session)
            if self.accessory is not None:
                self.accessory.active_char.set_value(0)

    def _schedule_session_cancel(self, session: SiriSession | None) -> None:
        if session is None or session.closed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _cancel() -> None:
            try:
                await session.cancel()
            except BaseException:
                _LOGGER.debug("Failed to cancel Siri session", exc_info=True)

        loop.create_task(_cancel(), name="cancel-active-siri")

    def _parse_target(self, raw: bytes | None) -> TargetConfiguration | None:
        if raw is None:
            return None
        data = tlv_decode(raw)
        identifier = read_u32(self._required(data, C.TARGET_IDENTIFIER, "target identifier"))
        if identifier == 0:
            raise ValueError("target identifier 0 is reserved")
        name: str | None = None
        if C.TARGET_NAME in data:
            try:
                name = data[C.TARGET_NAME].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("target name is not valid UTF-8") from exc
            if not name or len(name.encode("utf-8")) > _MAX_TARGET_NAME_BYTES:
                raise ValueError("target name is empty or too long")
        category = read_u16(data[C.TARGET_CATEGORY]) if C.TARGET_CATEGORY in data else None
        buttons: dict[int, ButtonConfiguration] = {}
        if C.TARGET_BUTTON_CONFIGURATION in data:
            for entry in decode_list(data[C.TARGET_BUTTON_CONFIGURATION], C.BUTTON_CONFIGURATION_ID):
                button_id_raw = self._required(entry, C.BUTTON_CONFIGURATION_ID, "button id")
                if len(button_id_raw) != 1:
                    raise ValueError("button id must be one byte")
                button_id = button_id_raw[0]
                button_type = read_u16(self._required(entry, C.BUTTON_CONFIGURATION_TYPE, "button type"))
                button_name_raw = entry.get(C.BUTTON_CONFIGURATION_NAME)
                button_name = None
                if button_name_raw is not None:
                    try:
                        button_name = button_name_raw.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise ValueError("button name is not valid UTF-8") from exc
                buttons[button_id] = ButtonConfiguration(button_id, button_type, button_name)
        return TargetConfiguration(identifier, name, category, buttons)

    def _build_audio_configuration(self, *, selected: bool) -> str:
        params = b"".join([
            tlv_encode((C.AUDIO_CHANNELS, 1)),
            tlv_encode((C.AUDIO_BITRATE, C.AUDIO_BITRATE_VARIABLE)),
            tlv_encode((C.AUDIO_SAMPLE_RATE, C.AUDIO_SAMPLE_RATE_16_KHZ)),
        ])
        if selected:
            params += tlv_encode((C.AUDIO_PACKET_TIME, FRAME_MS))
        codec = b"".join([
            tlv_encode((C.AUDIO_CODEC_TYPE, C.AUDIO_CODEC_OPUS)),
            tlv_encode((C.AUDIO_CODEC_PARAMETERS, params)),
        ])
        return base64.b64encode(tlv_encode((C.AUDIO_STREAM_CONFIGURATION, codec))).decode("ascii")

    @staticmethod
    def _target_to_dict(target: TargetConfiguration) -> dict[str, Any]:
        return {
            "target_identifier": target.target_identifier,
            "target_name": target.target_name,
            "target_category": target.target_category,
            "buttons": {str(k): asdict(v) for k, v in sorted(target.buttons.items())},
        }

    def _ensure_state_dir(self) -> None:
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name == "posix":
            try:
                os.chmod(self.state_dir, 0o700)
            except OSError:
                _LOGGER.warning("Could not restrict state directory permissions: %s", self.state_dir)

    def _save_targets(self) -> None:
        payload = {
            "version": 1,
            "active_identifier": self.active_identifier,
            "targets": {str(k): self._target_to_dict(v) for k, v in sorted(self.targets.items())},
        }
        self._ensure_state_dir()
        fd, tmp_name = tempfile.mkstemp(prefix="targets-", suffix=".tmp", dir=self.state_dir)
        try:
            if os.name == "posix":
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", closefd=True) as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.targets_path)
            if os.name == "posix":
                os.chmod(self.targets_path, 0o600)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def _load_targets(self) -> None:
        if not self.targets_path.exists():
            return
        try:
            payload = json.loads(self.targets_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("target state root must be an object")
            raw_targets = payload.get("targets", {})
            if not isinstance(raw_targets, dict) or len(raw_targets) > _MAX_TARGETS:
                raise ValueError("invalid target state")
            loaded: dict[int, TargetConfiguration] = {}
            for key, raw in raw_targets.items():
                if not isinstance(raw, dict):
                    raise ValueError("target entry must be an object")
                identifier = int(raw.get("target_identifier", key))
                if not 1 <= identifier <= 0xFFFFFFFF:
                    raise ValueError("persisted target identifier is out of range")
                raw_buttons = raw.get("buttons", {})
                if not isinstance(raw_buttons, dict):
                    raise ValueError("persisted button mapping must be an object")
                buttons: dict[int, ButtonConfiguration] = {}
                for button_id, button in raw_buttons.items():
                    if not isinstance(button, dict):
                        raise ValueError("persisted button entry must be an object")
                    parsed = ButtonConfiguration(
                        button_id=int(button.get("button_id", button_id)),
                        button_type=int(button["button_type"]),
                        button_name=button.get("button_name"),
                    )
                    if not 0 <= parsed.button_id <= 255 or not 0 <= parsed.button_type <= 0xFFFF:
                        raise ValueError("persisted button is out of range")
                    buttons[parsed.button_id] = parsed
                target = TargetConfiguration(
                    target_identifier=identifier,
                    target_name=raw.get("target_name"),
                    target_category=(int(raw["target_category"]) if raw.get("target_category") is not None else None),
                    buttons=buttons,
                )
                loaded[identifier] = target
            active = int(payload.get("active_identifier", 0))
            if active != 0 and active not in loaded:
                active = next(iter(sorted(loaded)), 0)
            self.targets = loaded
            self.active_identifier = active
            if os.name == "posix":
                try:
                    os.chmod(self.targets_path, 0o600)
                except OSError:
                    pass
        except Exception:
            stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
            quarantine = self.targets_path.with_name(f"targets.json.corrupt-{stamp}")
            try:
                self.targets_path.replace(quarantine)
                _LOGGER.exception("Invalid target state moved to %s", quarantine)
            except OSError:
                _LOGGER.exception("Failed to load target state from %s", self.targets_path)
            self.targets = {}
            self.active_identifier = 0

    @staticmethod
    def _decode_b64(value: Any, label: str) -> bytes:
        if not isinstance(value, str):
            raise ValueError(f"{label} must be base64 text")
        try:
            return base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"{label} is not valid base64") from exc

    @staticmethod
    def _required(mapping: dict[int, bytes], tag: int, label: str) -> bytes:
        try:
            value = mapping[tag]
        except KeyError as exc:
            raise ValueError(f"missing {label}") from exc
        if not value:
            raise ValueError(f"empty {label}")
        return value

    @classmethod
    def _one_byte(cls, mapping: dict[int, bytes], tag: int, label: str) -> int:
        value = cls._required(mapping, tag, label)
        if len(value) != 1:
            raise ValueError(f"{label} must be one byte")
        return value[0]
