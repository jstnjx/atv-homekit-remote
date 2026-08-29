from __future__ import annotations

import asyncio
import base64
from dataclasses import asdict, dataclass, field
import json
import logging
from pathlib import Path
import time
from typing import Any, AsyncIterable, Iterable

from .audio import FRAME_BYTES, FRAME_MS, SiriSession
from .constants import *
from .hap import AppleTVRemoteAccessory, SiriAccessoryDriver
from .hds import HDSConnection, HDSServer
from .tlv8 import decode as tlv_decode, decode_list, encode as tlv_encode, read_u16, read_u32, u16, u32, uint_le

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class RemoteConfig:
    name: str = "Voice Remote"
    username: str = "1A:2B:3C:4D:5E:6F"
    pincode: str = "031-45-154"
    port: int = 47129
    listen_address: str | None = None
    advertised_address: str | None = None
    hds_listen_address: str = "0.0.0.0"
    state_dir: str = ".atv-siri-py"


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


class AppleTVSiriRemote:
    """Pure-Python HomeKit Target Controller with Apple TV Siri audio support."""

    version = "0.1.0"

    def __init__(self, config: RemoteConfig | None = None) -> None:
        self.config = config or RemoteConfig()
        self.state_dir = Path(self.config.state_dir).expanduser().resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
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

    # ---------------- lifecycle ----------------

    async def start(self) -> None:
        if self._started:
            return
        if self.driver is None:
            loop = asyncio.get_running_loop()
            persist_file = str(self.state_dir / "hap.state")
            self.driver = SiriAccessoryDriver(
                address=self.config.advertised_address,
                advertised_address=self.config.advertised_address,
                listen_address=self.config.listen_address,
                port=self.config.port,
                pincode=bytearray(self.config.pincode.encode()),
                mac=self.config.username,
                persist_file=persist_file,
                connection_lost_callback=self._hap_connection_lost,
                loop=loop,
            )
            self.accessory = AppleTVRemoteAccessory(self.driver, self.config.name, self)
            self.driver.add_accessory(self.accessory)
        await self.hds.start()
        await self.driver.async_start()
        self._started = True
        _LOGGER.info("Apple TV Siri remote started; pair code: %s", self.config.pincode)

    async def stop(self) -> None:
        if not self._started:
            return
        for session in list(self.siri_sessions.values()):
            try:
                await session.cancel()
            except Exception:
                _LOGGER.debug("Could not cancel Siri session", exc_info=True)
        await self.hds.stop()
        if self.driver:
            await self.driver.async_stop()
        self._started = False

    async def __aenter__(self) -> "AppleTVSiriRemote":
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.stop()

    def _hap_objects(self) -> tuple[SiriAccessoryDriver, AppleTVRemoteAccessory]:
        if self.driver is None or self.accessory is None:
            raise RuntimeError("remote is not started; call await remote.start() first")
        return self.driver, self.accessory

    # ---------------- public state/control API ----------------

    @property
    def state(self) -> dict[str, Any]:
        return {
            "name": self.config.name,
            "active_identifier": self.active_identifier,
            "active": self.active_client is not None,
            "configured_targets": [self._target_to_dict(target) for target in self.targets.values()],
            "hds_targets": sorted(identifier for identifier, conn in self.hds_connections.items() if not conn.closed),
            "siri_ready": self.siri_ready,
        }

    @property
    def siri_ready(self) -> bool:
        connection = self.hds_connections.get(self.active_identifier)
        return bool(self.active_client and connection and not connection.closed and self._siri_advertised)

    def set_active_identifier(self, target_identifier: int) -> None:
        target_identifier = int(target_identifier)
        if target_identifier != 0 and target_identifier not in self.targets:
            raise ValueError(f"unknown target identifier {target_identifier}")
        if target_identifier == self.active_identifier:
            return
        self.active_identifier = target_identifier
        self.active_client = None
        _, accessory = self._hap_objects()
        accessory.active_identifier_char.set_value(target_identifier)
        accessory.active_char.set_value(0)
        self._save_targets()

    async def press(self, button: Button | str | int, *, hold_ms: int = 200, target: int | None = None) -> None:
        if isinstance(button, str):
            button = Button[button.upper()]
        else:
            button = Button(button)
        if target is not None and target != self.active_identifier:
            self.set_active_identifier(target)
        if button == Button.SIRI:
            raise ValueError("SIRI carries audio; use start_siri()/send_pcm()")
        self._send_button(button, ButtonState.DOWN)
        await asyncio.sleep(max(0, hold_ms) / 1000)
        self._send_button(button, ButtonState.UP)

    async def start_siri(self, *, target: int | None = None, wait_timeout: float = 4.0) -> SiriSession:
        try:
            await asyncio.wait_for(self._siri_lock.acquire(), wait_timeout)
        except TimeoutError as exc:
            raise RuntimeError("another Siri utterance is already in progress") from exc

        try:
            if target is not None and target != self.active_identifier:
                self.set_active_identifier(target)
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
            session = SiriSession(connection, target_identifier=identifier)
            session.start()
            asyncio.create_task(self._register_siri_session(session), name=f"siri-session-{identifier}")
            return session
        except BaseException:
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
        """Send 16 kHz mono signed PCM16 as one Siri utterance.

        Bytes are paced in 20 ms frames by default. Async iterables are assumed to be
        live and are forwarded at their natural arrival rate.
        """
        session = await self.start_siri(target=target)
        try:
            if isinstance(pcm, (bytes, bytearray, memoryview)):
                raw = bytes(pcm)
                for offset in range(0, len(raw), FRAME_BYTES):
                    chunk = raw[offset : offset + FRAME_BYTES]
                    await session.write(chunk)
                    if realtime and offset + FRAME_BYTES < len(raw):
                        await asyncio.sleep(FRAME_MS / 1000)
            elif hasattr(pcm, "__aiter__"):
                async for chunk in pcm:  # type: ignore[union-attr]
                    await session.write(chunk)
            else:
                for chunk in pcm:  # type: ignore[union-attr]
                    await session.write(chunk)
            await session.finish()
        except Exception:
            await session.cancel()
            raise

    async def recover_hds(self, *, phase_delay: float = 3.0) -> None:
        """Nudge tvOS to rebuild HDS by flipping the Target Control hardware capability.

        This keeps HAP pairing intact and increments the accessory configuration version
        on both transitions. It is a lighter-weight Python equivalent of the original
        bridge's buttons-only -> Siri republish recovery path.
        """
        self._siri_advertised = False
        driver, accessory = self._hap_objects()
        accessory.supported_target_char.set_value(self.target_supported_value())
        driver.config_changed()
        await asyncio.sleep(phase_delay)
        self._siri_advertised = True
        accessory.supported_target_char.set_value(self.target_supported_value())
        driver.config_changed()

    # ---------------- HomeKit characteristic handlers ----------------

    def target_supported_value(self) -> str:
        buttons = [
            Button.MENU, Button.PLAY_PAUSE, Button.TV_HOME, Button.SELECT,
            Button.ARROW_UP, Button.ARROW_RIGHT, Button.ARROW_DOWN, Button.ARROW_LEFT,
            Button.VOLUME_UP, Button.VOLUME_DOWN, Button.POWER, Button.GENERIC,
        ]
        if self._siri_advertised:
            buttons.append(Button.SIRI)
        button_data = b"".join(
            tlv_encode(
                (SUPPORTED_BUTTON_ID, 100 + int(button)),
                (SUPPORTED_BUTTON_TYPE, u16(int(button))),
            )
            for button in buttons
        )
        value = b"".join(
            [
                tlv_encode((TC_MAXIMUM_TARGETS, 10)),
                tlv_encode((TC_TICKS_PER_SECOND, uint_le(1000))),
                tlv_encode((TC_SUPPORTED_BUTTON_CONFIGURATION, button_data)),
                tlv_encode((TC_TYPE, 1 if self._siri_advertised else 0)),
            ]
        )
        return base64.b64encode(value).decode()

    def target_list_value(self) -> str:
        encoded = bytearray()
        for target in self.targets.values():
            parts = [
                tlv_encode((TARGET_IDENTIFIER, u32(target.target_identifier))),
            ]
            if target.target_name is not None:
                parts.append(tlv_encode((TARGET_NAME, target.target_name)))
            if target.target_category is not None:
                parts.append(tlv_encode((TARGET_CATEGORY, u16(target.target_category))))
            if target.buttons:
                button_data = bytearray()
                for button in target.buttons.values():
                    one = bytearray(
                        tlv_encode(
                            (BUTTON_CONFIGURATION_ID, button.button_id),
                            (BUTTON_CONFIGURATION_TYPE, u16(button.button_type)),
                        )
                    )
                    if button.button_name:
                        one.extend(tlv_encode((BUTTON_CONFIGURATION_NAME, button.button_name)))
                    button_data.extend(one)
                parts.append(tlv_encode((TARGET_BUTTON_CONFIGURATION, bytes(button_data))))
            encoded.extend(tlv_encode((TC_LIST_TARGET_CONFIGURATION, b"".join(parts))))
        return base64.b64encode(bytes(encoded)).decode()

    def hap_target_list_write(self, value: str, client: tuple[str, int] | None) -> str:
        # Pairing from Home/Apple TV is normally admin. Enforce when identity is available.
        driver, accessory = self._hap_objects()
        if client is not None and not driver.is_admin(client):
            raise PermissionError("TargetControlList is admin-only")
        data = tlv_decode(base64.b64decode(value))
        operation = TargetOperation(data[TC_LIST_OPERATION][0])
        config = self._parse_target(data.get(TC_LIST_TARGET_CONFIGURATION))
        if operation == TargetOperation.ADD:
            if not config:
                raise ValueError("ADD requires target configuration")
            self.targets[config.target_identifier] = config
            if self.active_identifier == 0:
                self.active_identifier = config.target_identifier
                accessory.active_identifier_char.set_value(self.active_identifier)
        elif operation == TargetOperation.UPDATE:
            if not config or config.target_identifier not in self.targets:
                raise ValueError("UPDATE references unknown target")
            current = self.targets[config.target_identifier]
            if config.target_name is not None:
                current.target_name = config.target_name
            if config.target_category is not None:
                current.target_category = config.target_category
            if config.buttons:
                current.buttons.update(config.buttons)
        elif operation == TargetOperation.REMOVE:
            if not config or config.target_identifier not in self.targets:
                raise ValueError("REMOVE references unknown target")
            current = self.targets[config.target_identifier]
            if config.buttons:
                for button_id in config.buttons:
                    current.buttons.pop(button_id, None)
            else:
                del self.targets[config.target_identifier]
                if self.active_identifier == config.target_identifier:
                    self.active_identifier = next(iter(self.targets), 0)
                    self.active_client = None
                    accessory.active_identifier_char.set_value(self.active_identifier)
                    accessory.active_char.set_value(0)
        elif operation == TargetOperation.RESET:
            if config is not None:
                raise ValueError("RESET must not include target configuration")
            self.targets.clear()
            self.active_identifier = 0
            self.active_client = None
            accessory.active_identifier_char.set_value(0)
            accessory.active_char.set_value(0)
        elif operation == TargetOperation.LIST:
            if config is not None:
                raise ValueError("LIST must not include target configuration")
        else:
            raise ValueError(f"unsupported target operation {operation}")
        self._save_targets()
        return self.target_list_value()

    def hap_active_write(self, value: Any, client: tuple[str, int] | None) -> None:
        if self.active_identifier == 0:
            raise ValueError("no target selected")
        if bool(value):
            if client is None:
                raise ValueError("Active write is missing HAP connection context")
            self.active_client = client
        elif client is None or client == self.active_client:
            self.active_client = None

    def supported_audio_value(self) -> str:
        return self._build_audio_configuration(selected=False)

    def selected_audio_value(self) -> str:
        return self._selected_audio

    def hap_selected_audio_write(self, value: str, client: tuple[str, int] | None) -> None:
        outer = tlv_decode(base64.b64decode(value))
        codec = tlv_decode(outer[AUDIO_STREAM_CONFIGURATION])
        parameters = tlv_decode(codec[AUDIO_CODEC_PARAMETERS])
        if codec[AUDIO_CODEC_TYPE][0] != AUDIO_CODEC_OPUS:
            raise ValueError("only Opus Siri input is supported")
        if parameters[AUDIO_CHANNELS][0] != 1 or parameters[AUDIO_SAMPLE_RATE][0] != AUDIO_SAMPLE_RATE_16_KHZ:
            raise ValueError("only 16 kHz mono Siri input is supported")
        self._selected_audio = value

    def supported_hds_value(self) -> str:
        value = tlv_encode(
            (HDS_TRANSFER_TRANSPORT_CONFIGURATION, tlv_encode((HDS_TRANSPORT_TYPE, HDS_HOMEKIT_DATA_STREAM)))
        )
        return base64.b64encode(value).decode()

    def setup_hds_value(self) -> str:
        return self._last_hds_setup

    def hap_setup_hds_write(self, value: str, client: tuple[str, int] | None) -> str:
        if client is None:
            raise ValueError("HDS setup needs HAP connection context")
        data = tlv_decode(base64.b64decode(value))
        if data[HDS_SESSION_COMMAND_TYPE][0] != HDS_START_SESSION:
            raise ValueError("unsupported HDS session command")
        if data[HDS_SESSION_TRANSPORT_TYPE][0] != HDS_HOMEKIT_DATA_STREAM:
            raise ValueError("unsupported HDS transport")
        controller_salt = data[HDS_CONTROLLER_KEY_SALT]
        if len(controller_salt) != 32:
            raise ValueError("invalid controller HDS salt")
        driver, _ = self._hap_objects()
        shared_secret = driver.shared_secret_for(client)
        if not shared_secret:
            raise RuntimeError("Pair Verify shared secret unavailable for HDS setup")
        prepared = self.hds.prepare_session(client, shared_secret, controller_salt)
        if self.hds.port is None:
            raise RuntimeError("HDS server is not listening")
        session_params = tlv_encode((HDS_TCP_LISTENING_PORT, u16(self.hds.port)))
        stripped = b"".join(
            [tlv_encode((HDS_SETUP_STATUS, 0)), tlv_encode((HDS_SETUP_SESSION_PARAMETERS, session_params))]
        )
        self._last_hds_setup = base64.b64encode(stripped).decode()
        response = stripped + tlv_encode((HDS_ACCESSORY_KEY_SALT, prepared.accessory_salt))
        return base64.b64encode(response).decode()

    # ---------------- internal protocol handling ----------------

    def _send_button(self, button: Button, state: ButtonState) -> None:
        if self.active_identifier == 0:
            raise RuntimeError("no target selected")
        if self.active_client is None:
            raise RuntimeError("selected target is not active")
        button_id = 100 + int(button)
        timestamp = int(time.time() * 1000)
        event = b"".join(
            [
                tlv_encode((BUTTON_EVENT_ID, button_id)),
                tlv_encode((BUTTON_EVENT_STATE, int(state))),
                tlv_encode((BUTTON_EVENT_TIMESTAMP, timestamp.to_bytes(8, "little"))),
                tlv_encode((BUTTON_EVENT_ACTIVE_IDENTIFIER, u32(self.active_identifier))),
            ]
        )
        self.last_button_event = base64.b64encode(event).decode()
        _, accessory = self._hap_objects()
        accessory.button_event_char.set_value(self.last_button_event)

    def _hds_whoami(self, identifier: int, connection: HDSConnection) -> None:
        old = self.hds_connections.get(identifier)
        if old and old is not connection and not old.closed:
            old.close()
        self.hds_connections[identifier] = connection
        _LOGGER.info("Apple TV target %d opened HDS", identifier)

    async def _hds_event(self, connection: HDSConnection, protocol: str, topic: str, message: dict[str, Any]) -> None:
        if protocol != HDS_PROTOCOL_DATA_SEND:
            return
        stream_id = int(message.get("streamId", -1))
        session = self.siri_sessions.get(stream_id)
        if not session:
            return
        if topic == HDS_TOPIC_ACK:
            await session.handle_ack(bool(message.get("endOfStream")))
            self.siri_sessions.pop(stream_id, None)
        elif topic == HDS_TOPIC_CLOSE:
            session.handle_close(int(message.get("reason", HDSCloseReason.UNEXPECTED_FAILURE)))
            self.siri_sessions.pop(stream_id, None)

    async def _register_siri_session(self, session: SiriSession) -> None:
        try:
            if session._open_task:
                await session._open_task
            if session.stream_id is not None:
                self.siri_sessions[session.stream_id] = session
                await session._done.wait()
                self.siri_sessions.pop(session.stream_id, None)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.debug("Siri session failed during registration", exc_info=True)
        finally:
            if session.stream_id is not None:
                self.siri_sessions.pop(session.stream_id, None)
            if self._siri_lock.locked():
                self._siri_lock.release()

    def _hds_connection_closed(self, connection: HDSConnection) -> None:
        if connection.target_identifier is not None:
            current = self.hds_connections.get(connection.target_identifier)
            if current is connection:
                self.hds_connections.pop(connection.target_identifier, None)
        for stream_id, session in list(self.siri_sessions.items()):
            if session.connection is connection:
                session.handle_close(HDSCloseReason.UNEXPECTED_FAILURE)
                self.siri_sessions.pop(stream_id, None)

    def _hap_connection_lost(self, client: tuple[str, int]) -> None:
        self.hds.close_for_client(client)
        if client == self.active_client:
            self.active_client = None
            if self.accessory is not None:
                self.accessory.active_char.set_value(0)

    # ---------------- target/audio encoding/persistence ----------------

    def _parse_target(self, raw: bytes | None) -> TargetConfiguration | None:
        if raw is None:
            return None
        data = tlv_decode(raw)
        identifier = read_u32(data[TARGET_IDENTIFIER])
        buttons: dict[int, ButtonConfiguration] = {}
        if TARGET_BUTTON_CONFIGURATION in data:
            for entry in decode_list(data[TARGET_BUTTON_CONFIGURATION], BUTTON_CONFIGURATION_ID):
                button_id = entry[BUTTON_CONFIGURATION_ID][0]
                button_type = read_u16(entry[BUTTON_CONFIGURATION_TYPE])
                name = entry.get(BUTTON_CONFIGURATION_NAME)
                buttons[button_id] = ButtonConfiguration(
                    button_id=button_id,
                    button_type=button_type,
                    button_name=name.decode() if name else None,
                )
        return TargetConfiguration(
            target_identifier=identifier,
            target_name=data[TARGET_NAME].decode() if TARGET_NAME in data else None,
            target_category=read_u16(data[TARGET_CATEGORY]) if TARGET_CATEGORY in data else None,
            buttons=buttons,
        )

    def _build_audio_configuration(self, *, selected: bool) -> str:
        params = b"".join(
            [
                tlv_encode((AUDIO_CHANNELS, 1)),
                tlv_encode((AUDIO_BITRATE, AUDIO_BITRATE_VARIABLE)),
                tlv_encode((AUDIO_SAMPLE_RATE, AUDIO_SAMPLE_RATE_16_KHZ)),
            ]
        )
        if selected:
            params += tlv_encode((AUDIO_PACKET_TIME, 20))
        codec = b"".join(
            [
                tlv_encode((AUDIO_CODEC_TYPE, AUDIO_CODEC_OPUS)),
                tlv_encode((AUDIO_CODEC_PARAMETERS, params)),
            ]
        )
        return base64.b64encode(tlv_encode((AUDIO_STREAM_CONFIGURATION, codec))).decode()

    def _target_to_dict(self, target: TargetConfiguration) -> dict[str, Any]:
        return {
            "target_identifier": target.target_identifier,
            "target_name": target.target_name,
            "target_category": target.target_category,
            "buttons": {str(k): asdict(v) for k, v in target.buttons.items()},
        }

    def _save_targets(self) -> None:
        payload = {
            "active_identifier": self.active_identifier,
            "targets": {str(k): self._target_to_dict(v) for k, v in self.targets.items()},
        }
        tmp = self.targets_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.targets_path)

    def _load_targets(self) -> None:
        if not self.targets_path.exists():
            return
        try:
            payload = json.loads(self.targets_path.read_text(encoding="utf-8"))
            self.active_identifier = int(payload.get("active_identifier", 0))
            for key, raw in payload.get("targets", {}).items():
                buttons = {
                    int(button_id): ButtonConfiguration(**button)
                    for button_id, button in raw.get("buttons", {}).items()
                }
                target = TargetConfiguration(
                    target_identifier=int(raw.get("target_identifier", key)),
                    target_name=raw.get("target_name"),
                    target_category=raw.get("target_category"),
                    buttons=buttons,
                )
                self.targets[target.target_identifier] = target
            if self.active_identifier not in self.targets:
                self.active_identifier = next(iter(self.targets), 0)
        except Exception:
            _LOGGER.exception("Failed to load target state from %s", self.targets_path)
            self.targets = {}
            self.active_identifier = 0
