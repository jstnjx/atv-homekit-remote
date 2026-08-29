from __future__ import annotations

import argparse
import asyncio
import logging
import os

from .http_api import CompatibilityHTTPServer
from .remote import AppleTVSiriRemote, RemoteConfig


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="atv-siri", description="Pure-Python Apple TV HomeKit Siri remote")
    p.add_argument("--name", default=_env("HAP_NAME", "Voice Remote"))
    p.add_argument("--username", default=_env("HAP_USERNAME", "1A:2B:3C:4D:5E:6F"))
    p.add_argument("--pincode", default=_env("PINCODE", "031-45-154"))
    p.add_argument("--hap-port", type=int, default=int(_env("HAP_PORT", "47129")))
    p.add_argument("--hap-bind", default=os.environ.get("HAP_BIND"))
    p.add_argument("--state-dir", default=_env("HAP_STORAGE", ".atv-siri-py"))
    p.add_argument("--ctrl-bind", default=_env("CTRL_BIND", "127.0.0.1"))
    p.add_argument("--ctrl-port", type=int, default=int(_env("CTRL_PORT", "8477")))
    p.add_argument("--no-http", action="store_true", help="disable compatibility HTTP control API")
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
        state_dir=args.state_dir,
    )
    remote = AppleTVSiriRemote(config)
    api = None if args.no_http else CompatibilityHTTPServer(remote, host=args.ctrl_bind, port=args.ctrl_port)
    await remote.start()
    if api:
        await api.start()
        logging.getLogger(__name__).info("Control API listening on http://%s:%d", args.ctrl_bind, args.ctrl_port)
    logging.getLogger(__name__).info("Pair '%s' in Apple Home with code %s", args.name, args.pincode)
    try:
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
