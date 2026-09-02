from __future__ import annotations

import asyncio
import hmac
import ipaddress
from pathlib import Path
import wave
from typing import Any, AsyncIterator

from aiohttp import web

from .remote import AppleTVHomeKitRemote

_DEFAULT_MAX_AUDIO_BYTES = 16 * 1024 * 1024


def _is_loopback_bind(host: str) -> bool:
    normalized = host.strip().strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


class RemoteHTTPServer:
    """HTTP control API for Apple TV HomeKit Remote."""

    def __init__(
        self,
        remote: AppleTVHomeKitRemote,
        *,
        host: str = "127.0.0.1",
        port: int = 8477,
        token: str | None = None,
        allow_file_api: bool = False,
        max_audio_bytes: int = _DEFAULT_MAX_AUDIO_BYTES,
        utterance_timeout: float = 60.0,
    ) -> None:
        if not 1 <= int(port) <= 65535:
            raise ValueError("HTTP port must be in range 1..65535")
        if max_audio_bytes < 640 or max_audio_bytes > 512 * 1024 * 1024:
            raise ValueError("max_audio_bytes must be between 640 bytes and 512 MiB")
        if utterance_timeout <= 0 or utterance_timeout > 600:
            raise ValueError("utterance_timeout must be in range (0, 600]")
        if token is not None and len(token) < 16:
            raise ValueError("HTTP bearer token must be at least 16 characters")
        if not _is_loopback_bind(host) and token is None:
            raise ValueError("a bearer token is required when the HTTP API binds beyond loopback")

        self.remote = remote
        self.host = host
        self.port = int(port)
        self.token = token
        self.allow_file_api = allow_file_api
        self.max_audio_bytes = int(max_audio_bytes)
        self.utterance_timeout = float(utterance_timeout)

        self.app = web.Application(client_max_size=self.max_audio_bytes, middlewares=[self._auth_middleware])
        routes = [
            web.get("/healthz", self.healthz),
            web.get("/state", self.state),
            web.post("/active/{identifier}", self.active),
            web.post("/press/{button}", self.press),
            web.post("/recover", self.recover),
            web.post("/siri/stream", self.siri_stream),
        ]
        if self.allow_file_api:
            routes.append(web.post("/siri/file", self.siri_file))
        self.app.add_routes(routes)
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler: Any) -> web.StreamResponse:
        if request.path == "/healthz" or self.token is None:
            return await handler(request)
        expected = f"Bearer {self.token}"
        supplied = request.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied, expected):
            raise web.HTTPUnauthorized(
                text='{"error":"unauthorized"}',
                content_type="application/json",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await handler(request)

    async def start(self) -> None:
        if self._runner is not None:
            return
        runner = web.AppRunner(self.app, access_log=None)
        try:
            await runner.setup()
            site = web.TCPSite(runner, self.host, self.port)
            await site.start()
        except BaseException:
            await runner.cleanup()
            raise
        self._runner = runner
        self._site = site

    async def stop(self) -> None:
        runner = self._runner
        self._runner = None
        self._site = None
        if runner is not None:
            await runner.cleanup()

    @staticmethod
    def _json(data: Any, *, status: int = 200) -> web.Response:
        return web.json_response(data, status=status, headers={"Cache-Control": "no-store"})

    def _state_response(self) -> web.Response:
        return self._json(self.remote.state)

    async def healthz(self, request: web.Request) -> web.Response:
        del request
        status = 200 if self.remote.started else 503
        return self._json({"ok": self.remote.started, "siri_ready": self.remote.siri_ready, "version": self.remote.version}, status=status)

    async def state(self, request: web.Request) -> web.Response:
        del request
        return self._state_response()

    async def active(self, request: web.Request) -> web.Response:
        try:
            self.remote.set_active_identifier(int(request.match_info["identifier"]))
            return self._state_response()
        except ValueError as exc:
            return self._error(exc, 400)
        except RuntimeError as exc:
            return self._error(exc, 409)

    async def press(self, request: web.Request) -> web.Response:
        try:
            target = self._query_int(request, "target")
            hold = self._query_int(request, "hold_ms", default=200)
            await self.remote.press(request.match_info["button"], target=target, hold_ms=hold if hold is not None else 200)
            return self._state_response()
        except ValueError as exc:
            return self._error(exc, 400)
        except RuntimeError as exc:
            return self._error(exc, 409)
        except (ConnectionError, OSError) as exc:
            return self._error(exc, 503)

    async def recover(self, request: web.Request) -> web.Response:
        try:
            delay = float(request.query.get("phase_delay", "3"))
            async with asyncio.timeout(min(self.utterance_timeout, max(10.0, delay + 10.0))):
                await self.remote.recover_hds(phase_delay=delay)
            return self._state_response()
        except ValueError as exc:
            return self._error(exc, 400)
        except TimeoutError as exc:
            return self._error(exc, 504)
        except RuntimeError as exc:
            return self._error(exc, 409)

    async def siri_stream(self, request: web.Request) -> web.Response:
        try:
            target = self._query_int(request, "target")
            content_length = request.content_length
            if content_length is not None and content_length > self.max_audio_bytes:
                raise web.HTTPRequestEntityTooLarge(max_size=self.max_audio_bytes, actual_size=content_length)
            async with asyncio.timeout(self.utterance_timeout):
                await self.remote.send_pcm(self._bounded_audio_chunks(request), target=target, realtime=False)
            return self._state_response()
        except web.HTTPException:
            raise
        except ValueError as exc:
            return self._error(exc, 400)
        except TimeoutError as exc:
            return self._error(exc, 504)
        except RuntimeError as exc:
            return self._error(exc, 409)
        except (ConnectionError, OSError) as exc:
            return self._error(exc, 503)

    async def siri_file(self, request: web.Request) -> web.Response:
        if not self.allow_file_api:
            raise web.HTTPNotFound()
        file_name = request.query.get("file")
        if not file_name:
            return self._json({"error": "missing ?file=/path/to/audio.wav"}, status=400)
        try:
            target = self._query_int(request, "target")
            path = Path(file_name).expanduser().resolve(strict=True)
            if not path.is_file():
                raise ValueError("file path does not reference a regular file")
            if path.stat().st_size > self.max_audio_bytes + 4096:
                raise ValueError("WAV file exceeds configured audio size limit")
            with wave.open(str(path), "rb") as wav:
                if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 16_000):
                    raise ValueError("WAV must be 16 kHz, mono, signed PCM16")
                frame_bytes = wav.getnframes() * wav.getsampwidth() * wav.getnchannels()
                if frame_bytes > self.max_audio_bytes:
                    raise ValueError("WAV payload exceeds configured audio size limit")
                pcm = wav.readframes(wav.getnframes())
            async with asyncio.timeout(self.utterance_timeout):
                await self.remote.send_pcm(pcm, target=target, realtime=True)
            return self._state_response()
        except FileNotFoundError as exc:
            return self._error(exc, 404)
        except (OSError, wave.Error, ValueError) as exc:
            return self._error(exc, 400)
        except TimeoutError as exc:
            return self._error(exc, 504)
        except RuntimeError as exc:
            return self._error(exc, 409)
        except ConnectionError as exc:
            return self._error(exc, 503)

    async def _bounded_audio_chunks(self, request: web.Request) -> AsyncIterator[bytes]:
        total = 0
        async for chunk in request.content.iter_chunked(4096):
            total += len(chunk)
            if total > self.max_audio_bytes:
                raise web.HTTPRequestEntityTooLarge(max_size=self.max_audio_bytes, actual_size=total)
            yield bytes(chunk)

    @staticmethod
    def _query_int(request: web.Request, name: str, *, default: int | None = None) -> int | None:
        raw = request.query.get(name)
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError as exc:
            raise ValueError(f"query parameter {name!r} must be an integer") from exc

    def _error(self, exc: BaseException, status: int) -> web.Response:
        return self._json({"error": str(exc), "state": self.remote.state}, status=status)
