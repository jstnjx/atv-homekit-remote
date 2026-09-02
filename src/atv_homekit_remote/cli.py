from __future__ import annotations

import argparse
import asyncio
import logging
import os

from .http_api import RemoteHTTPServer
from .remote import AppleTVHomeKitRemote, RemoteConfig

_LOGGER = logging.getLogger(__name__)


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in {None, ""} else default


def _env_int(name: str, default: int) -> int:
    value = _env(name)
    return int(value) if value is not None else default


def _env_float(name: str, default: float) -> float:
    value = _env(name)
    return float(value) if value is not None else default


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="atv-homekit-remote",
        description="Pure-Python Apple TV HomeKit remote with Siri voice support",
    )
    p.add_argument("--name", default=_env("HAP_NAME", "Apple TV HomeKit Remote"))
    p.add_argument("--username", default=_env("HAP_USERNAME"), help="fixed HomeKit accessory id; generated if omitted")
    p.add_argument(
        "--pincode",
        default=_env("HAP_PINCODE", _env("PINCODE")),
        help="fixed HomeKit PIN; generated if omitted",
    )
    p.add_argument("--hap-port", type=int, default=_env_int("HAP_PORT", 47129))
    p.add_argument("--hap-bind", default=_env("HAP_BIND"), help="local IP address for the HAP listener")
    p.add_argument(
        "--hap-advertise",
        default=_env("HAP_ADVERTISED_ADDRESS"),
        help="IP address advertised over mDNS",
    )
    p.add_argument("--hds-bind", default=_env("HDS_BIND", "0.0.0.0"), help="local IP address for HomeKit Data Stream")
    p.add_argument("--state-dir", default=_env("HAP_STORAGE", ".atv-homekit-remote"))
    p.add_argument("--ctrl-bind", default=_env("CTRL_BIND", "127.0.0.1"))
    p.add_argument("--ctrl-port", type=int, default=_env_int("CTRL_PORT", 8477))
    p.add_argument(
        "--ctrl-token",
        default=_env("CTRL_TOKEN"),
        help="bearer token; required for non-loopback control API",
    )
    p.add_argument(
        "--allow-file-api",
        action="store_true",
        default=_env("ALLOW_FILE_API", "0") == "1",
        help="enable /siri/file local-path endpoint (disabled by default)",
    )
    p.add_argument("--max-audio-bytes", type=int, default=_env_int("MAX_AUDIO_BYTES", 16 * 1024 * 1024))
    p.add_argument("--utterance-timeout", type=float, default=_env_float("UTTERANCE_TIMEOUT", 60.0))
    p.add_argument("--no-http", action="store_true", help="disable HTTP control API")
    p.add_argument("-v", "--verbose", action="count", default=0)
    return p


async def _main(args: argparse.Namespace) -> None:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = RemoteConfig(
        name=args.name,
        username=args.username,
        pincode=args.pincode,
        port=args.hap_port,
        listen_address=args.hap_bind,
        advertised_address=args.hap_advertise,
        hds_listen_address=args.hds_bind,
        state_dir=args.state_dir,
    )
    remote = AppleTVHomeKitRemote(config)
    api = None
    if not args.no_http:
        api = RemoteHTTPServer(
            remote,
            host=args.ctrl_bind,
            port=args.ctrl_port,
            token=args.ctrl_token,
            allow_file_api=args.allow_file_api,
            max_audio_bytes=args.max_audio_bytes,
            utterance_timeout=args.utterance_timeout,
        )
    try:
        await remote.start()
        if api:
            await api.start()
            auth = "Bearer authentication enabled" if args.ctrl_token else "loopback only"
            _LOGGER.info("Control API listening on http://%s:%d (%s)", args.ctrl_bind, args.ctrl_port, auth)
        _LOGGER.info("Pair %r in Apple Home with code %s", args.name, remote.pincode)
        await asyncio.Event().wait()
    finally:
        if api:
            await api.stop()
        await remote.stop()


def main() -> None:
    args = parser().parse_args()
    try:
        asyncio.run(_main(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
