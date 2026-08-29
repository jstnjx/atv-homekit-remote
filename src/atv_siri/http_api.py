from __future__ import annotations

import asyncio
import wave
from pathlib import Path
from typing import Any

from aiohttp import web

from .remote import AppleTVSiriRemote


class CompatibilityHTTPServer:
    """Optional HTTP API compatible with the original Node bridge endpoints."""

    def __init__(self, remote: AppleTVSiriRemote, *, host: str = "127.0.0.1", port: int = 8477) -> None:
        self.remote = remote
        self.host = host
        self.port = port
        self.app = web.Application()
        self.app.add_routes(
            [
                web.get("/state", self.state),
                web.route("*", "/active/{identifier}", self.active),
                web.route("*", "/press/{button}", self.press),
                web.route("*", "/recover", self.recover),
                web.post("/siri/stream", self.siri_stream),
                web.route("*", "/siri/file", self.siri_file),
            ]
        )
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    async def start(self) -> None:
        if self._runner:
            return
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
        self._runner = None
        self._site = None

    async def _json_call(self, coro: Any) -> web.Response:
        try:
            if asyncio.iscoroutine(coro):
                await coro
            return web.json_response(self.remote.state)
        except (ValueError, RuntimeError, TimeoutError) as exc:
            return web.json_response({"error": str(exc), "state": self.remote.state}, status=409)

    async def state(self, request: web.Request) -> web.Response:
        return web.json_response(self.remote.state)

    async def active(self, request: web.Request) -> web.Response:
        try:
            self.remote.set_active_identifier(int(request.match_info["identifier"]))
            return web.json_response(self.remote.state)
        except (ValueError, RuntimeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def press(self, request: web.Request) -> web.Response:
        target = request.query.get("target")
        hold = int(request.query.get("hold_ms", "200"))
        return await self._json_call(
            self.remote.press(
                request.match_info["button"],
                target=int(target) if target is not None else None,
                hold_ms=hold,
            )
        )

    async def recover(self, request: web.Request) -> web.Response:
        delay = float(request.query.get("phase_delay", "3"))
        return await self._json_call(self.remote.recover_hds(phase_delay=delay))

    async def siri_stream(self, request: web.Request) -> web.Response:
        target = request.query.get("target")
        try:
            await self.remote.send_pcm(
                request.content.iter_chunked(4096),
                target=int(target) if target is not None else None,
            )
            return web.json_response(self.remote.state)
        except (ValueError, RuntimeError, TimeoutError, ConnectionError) as exc:
            return web.json_response({"error": str(exc), "state": self.remote.state}, status=409)

    async def siri_file(self, request: web.Request) -> web.Response:
        file_name = request.query.get("file")
        if not file_name:
            return web.json_response({"error": "missing ?file=/path/to/audio.wav"}, status=400)
        target = request.query.get("target")
        path = Path(file_name).expanduser()
        try:
            with wave.open(str(path), "rb") as wav:
                if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 16000):
                    raise ValueError("WAV must be 16 kHz, mono, signed PCM16")
                pcm = wav.readframes(wav.getnframes())
            await self.remote.send_pcm(pcm, target=int(target) if target is not None else None, realtime=True)
            return web.json_response(self.remote.state)
        except (OSError, wave.Error, ValueError, RuntimeError, TimeoutError) as exc:
            return web.json_response({"error": str(exc), "state": self.remote.state}, status=409)
