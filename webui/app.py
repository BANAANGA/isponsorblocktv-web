import asyncio
import hmac
import json
import logging
import os
import secrets
import sys
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiohttp
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from iSponsorBlockTV import api_helpers
from iSponsorBlockTV.helpers import Config
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from starlette.middleware.sessions import SessionMiddleware

from .activity import ActivityStore

logger = logging.getLogger("isponsorblocktv.webui")

DATA_DIR = Path(os.environ.get("ISPBTV_DATA_DIR", "./data")).expanduser().resolve()
STATIC_DIR = Path(__file__).parent / "static"
VALID_CATEGORIES = {
    "sponsor",
    "selfpromo",
    "exclusive_access",
    "interaction",
    "intro",
    "outro",
    "preview",
    "filler",
    "music_offtopic",
    "hook",
}
DEFAULT_CONFIG: dict[str, Any] = {
    "devices": [],
    "skip_categories": ["sponsor"],
    "skip_count_tracking": True,
    "mute_ads": True,
    "skip_ads": True,
    "minimum_skip_length": 1,
    "auto_play": True,
    "join_name": "iSponsorBlockTV",
    "apikey": "",
    "channel_whitelist": [],
    "use_proxy": False,
    "sponsorblock_api_url": "https://sponsor.ajay.app/api/",
}
DEFAULT_RUNTIME_OPTIONS: dict[str, Any] = {
    "debug": False,
    "http_tracing": False,
    "max_history_videos": 1000,
    "record_video_history": True,
}


class DeviceConfig(BaseModel):
    screen_id: str = Field(min_length=1, max_length=512)
    name: str = Field(default="", max_length=200)
    offset: float = Field(default=0, ge=-60000, le=60000)

    @field_validator("screen_id")
    @classmethod
    def screen_id_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Screen ID cannot be empty")
        return value.strip()


class ChannelConfig(BaseModel):
    id: str = Field(min_length=1, max_length=256)
    name: str = Field(default="", max_length=200)


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    devices: list[DeviceConfig] = Field(default_factory=list, max_length=100)
    skip_categories: list[str] = Field(default_factory=lambda: ["sponsor"], max_length=10)
    skip_count_tracking: bool = True
    mute_ads: bool = True
    skip_ads: bool = True
    minimum_skip_length: float = Field(default=1, ge=0, le=3600)
    auto_play: bool = True
    join_name: str = Field(default="iSponsorBlockTV", min_length=1, max_length=200)
    apikey: str = Field(default="", max_length=512)
    channel_whitelist: list[ChannelConfig] = Field(default_factory=list, max_length=1000)
    use_proxy: bool = False
    debug: bool = False
    http_tracing: bool = False
    max_history_videos: int = Field(default=1000, ge=100, le=10000)
    record_video_history: bool = True
    sponsorblock_api_url: str = Field(
        default="https://sponsor.ajay.app/api/", min_length=1, max_length=2048
    )

    @field_validator("skip_categories")
    @classmethod
    def valid_categories(cls, values: list[str]) -> list[str]:
        invalid = set(values) - VALID_CATEGORIES
        if invalid:
            raise ValueError(f"Unsupported skip categories: {', '.join(sorted(invalid))}")
        return list(dict.fromkeys(values))

    @field_validator("sponsorblock_api_url")
    @classmethod
    def valid_api_url(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith(("https://", "http://")):
            raise ValueError("SponsorBlock API URL must start with http:// or https://")
        return value.rstrip("/") + "/"

    @model_validator(mode="after")
    def validate_whitelist(self) -> "Settings":
        if self.channel_whitelist and not self.apikey:
            raise ValueError("A YouTube API key is required when the channel whitelist is used")
        return self


class LoginPayload(BaseModel):
    username: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=1, max_length=1024)


class PairPayload(BaseModel):
    pairing_code: str = Field(min_length=12, max_length=16)
    name: str = Field(default="", max_length=200)


class ChannelSearchPayload(BaseModel):
    query: str = Field(min_length=1, max_length=200)


def _session_secret() -> str:
    provided = os.environ.get("WEB_SESSION_SECRET")
    if provided:
        return provided
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    secret_file = DATA_DIR / ".web-session-secret"
    try:
        secret = secret_file.read_text(encoding="utf-8").strip()
        if secret:
            return secret
    except FileNotFoundError:
        pass
    secret = secrets.token_urlsafe(48)
    descriptor = os.open(secret_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(secret)
    return secret


class ServiceManager:
    def __init__(self) -> None:
        self.process: asyncio.subprocess.Process | None = None
        self.log_lines: deque[str] = deque(maxlen=5000)
        self.reader_task: asyncio.Task[None] | None = None
        self.started_at: float | None = None
        self.lock = asyncio.Lock()
        self.log_path = DATA_DIR / "service.log"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        for path in (self.log_path.with_suffix(".log.1"), self.log_path):
            try:
                with path.open(encoding="utf-8") as source:
                    self.log_lines.extend(line.rstrip("\n") for line in source)
            except FileNotFoundError:
                continue

    def _append_log(self, line: str) -> None:
        try:
            if self.log_path.exists() and self.log_path.stat().st_size >= 5 * 1024 * 1024:
                os.replace(self.log_path, self.log_path.with_suffix(".log.1"))
            with self.log_path.open("a", encoding="utf-8") as output:
                output.write(line + "\n")
        except OSError:
            logger.exception("Could not persist service log output")

    def clear_logs(self) -> None:
        self.log_lines.clear()
        for path in (self.log_path, self.log_path.with_suffix(".log.1")):
            path.unlink(missing_ok=True)

    async def start(self) -> None:
        async with self.lock:
            if self.process and self.process.returncode is None:
                return
            config = load_config()
            if not config["devices"]:
                raise HTTPException(status_code=400, detail="Pair or add a device before starting")
            options = load_runtime_options()
            command = [
                sys.executable,
                "-u",
                "-m",
                "webui.runner",
                "--data",
                str(DATA_DIR),
            ]
            if options["debug"]:
                command.append("--debug")
            if options["http_tracing"]:
                command.append("--http-tracing")
            self.process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env={
                    **os.environ,
                    "PYTHONUNBUFFERED": "1",
                    "TERM": "dumb",
                    "ISPBTV_MAX_HISTORY_VIDEOS": str(options["max_history_videos"]),
                    "ISPBTV_RECORD_VIDEO_HISTORY": str(options["record_video_history"]).lower(),
                },
            )
            self.started_at = time.time()
            self.reader_task = asyncio.create_task(self._read_output(self.process))
            self.log_lines.append("[webui] iSponsorBlockTV service started")
            self._append_log("[webui] iSponsorBlockTV service started")

    async def stop(self) -> None:
        async with self.lock:
            process = self.process
            if not process or process.returncode is not None:
                return
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
            if self.reader_task:
                await self.reader_task
            self.started_at = None
            self.log_lines.append("[webui] iSponsorBlockTV service stopped")
            self._append_log("[webui] iSponsorBlockTV service stopped")

    async def restart(self) -> None:
        was_running = self.process is not None and self.process.returncode is None
        if was_running:
            await self.stop()
            if load_config()["devices"]:
                await self.start()

    async def _read_output(self, process: asyncio.subprocess.Process) -> None:
        assert process.stdout is not None
        while line := await process.stdout.readline():
            text = line.decode("utf-8", errors="replace").rstrip()
            self.log_lines.append(text)
            self._append_log(text)
        await process.wait()
        if process.returncode:
            message = f"[webui] Service exited with code {process.returncode}"
            self.log_lines.append(message)
            self._append_log(message)
            self.started_at = None

    def status(self) -> dict[str, Any]:
        running = self.process is not None and self.process.returncode is None
        return {
            "running": running,
            "pid": self.process.pid if running and self.process else None,
            "started_at": self.started_at,
            "exit_code": self.process.returncode if self.process else None,
        }


service = ServiceManager()
activity_store = ActivityStore(DATA_DIR / "activity.sqlite3")
login_attempts: dict[str, deque[float]] = defaultdict(deque)
ADMIN_USERNAME = os.environ.get("WEB_USERNAME", "")
ADMIN_PASSWORD = os.environ.get("WEB_PASSWORD", "")


def load_config() -> dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / "config.json"
    if not path.exists():
        return DEFAULT_CONFIG.copy()
    with path.open(encoding="utf-8") as source:
        stored = json.load(source)
    return {**DEFAULT_CONFIG, **stored}


def load_runtime_options() -> dict[str, Any]:
    path = DATA_DIR / "webui-settings.json"
    if not path.exists():
        return DEFAULT_RUNTIME_OPTIONS.copy()
    with path.open(encoding="utf-8") as source:
        options = json.load(source)
    return {**DEFAULT_RUNTIME_OPTIONS, **options}


def load_settings() -> Settings:
    return Settings.model_validate({**load_config(), **load_runtime_options()})


def _constant_time_equal(first: str, second: str) -> bool:
    return hmac.compare_digest(first.encode("utf-8"), second.encode("utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(payload, output, indent=4)
        output.write("\n")
    os.replace(temp_path, path)


def save_config(settings: Settings) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _write_json(
        DATA_DIR / "config.json",
        settings.model_dump(
            exclude={"debug", "http_tracing", "max_history_videos", "record_video_history"}
        ),
    )
    _write_json(
        DATA_DIR / "webui-settings.json",
        {
            "debug": settings.debug,
            "http_tracing": settings.http_tracing,
            "max_history_videos": settings.max_history_videos,
            "record_video_history": settings.record_video_history,
        },
    )
    activity_store.set_history_limit(settings.max_history_videos)


async def require_auth(request: Request) -> None:
    if request.session.get("username") != ADMIN_USERNAME or not ADMIN_USERNAME:
        raise HTTPException(status_code=401, detail="Authentication required")


async def require_csrf(request: Request, _: None = Depends(require_auth)) -> None:
    expected = request.session.get("csrf")
    supplied = request.headers.get("x-csrf-token", "")
    if not expected or not _constant_time_equal(expected, supplied):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not ADMIN_USERNAME or not ADMIN_PASSWORD:
        raise RuntimeError("Set WEB_USERNAME and WEB_PASSWORD before starting the web interface")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    activity_store.set_history_limit(load_runtime_options()["max_history_videos"])
    if load_config()["devices"]:
        try:
            await service.start()
        except Exception:
            logger.exception("Could not start iSponsorBlockTV")
    yield
    await service.stop()


app = FastAPI(
    title="iSponsorBlockTV Web",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret(),
    session_cookie="ispbtv_session",
    max_age=12 * 60 * 60,
    same_site="strict",
    https_only=os.environ.get("COOKIE_SECURE", "").lower() in {"1", "true", "yes"},
)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    elif request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data: https://i.ytimg.com; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/auth/status")
async def auth_status(request: Request) -> dict[str, Any]:
    authenticated = request.session.get("username") == ADMIN_USERNAME and bool(ADMIN_USERNAME)
    return {
        "authenticated": authenticated,
        "username": ADMIN_USERNAME if authenticated else None,
        "csrf_token": request.session.get("csrf") if authenticated else None,
    }


@app.post("/api/auth/login")
async def login(payload: LoginPayload, request: Request, response: Response) -> dict[str, str]:
    client = request.client.host if request.client else "unknown"
    attempts = login_attempts[client]
    now = time.monotonic()
    while attempts and now - attempts[0] > 300:
        attempts.popleft()
    if len(attempts) >= 5:
        raise HTTPException(status_code=429, detail="Too many login attempts; wait five minutes")
    username_ok = _constant_time_equal(payload.username, ADMIN_USERNAME)
    password_ok = _constant_time_equal(payload.password, ADMIN_PASSWORD)
    if not (username_ok and password_ok):
        attempts.append(now)
        raise HTTPException(status_code=401, detail="Invalid username or password")
    attempts.clear()
    request.session.clear()
    csrf_token = secrets.token_urlsafe(32)
    request.session.update({"username": ADMIN_USERNAME, "csrf": csrf_token})
    response.headers["Cache-Control"] = "no-store"
    return {"username": ADMIN_USERNAME, "csrf_token": csrf_token}


@app.post("/api/auth/logout", dependencies=[Depends(require_csrf)])
async def logout(request: Request) -> dict[str, bool]:
    request.session.clear()
    return {"success": True}


@app.get("/api/config", dependencies=[Depends(require_auth)])
async def get_config() -> dict[str, Any]:
    try:
        settings = load_settings()
    except (ValueError, OSError) as error:
        raise HTTPException(status_code=500, detail=f"Could not load configuration: {error}") from error
    return settings.model_dump()


@app.put("/api/config", dependencies=[Depends(require_csrf)])
async def update_config(settings: Settings) -> dict[str, Any]:
    try:
        save_config(settings)
    except OSError as error:
        raise HTTPException(status_code=500, detail=f"Could not save configuration: {error}") from error
    await service.restart()
    return {"success": True, "service": service.status()}


@app.get("/api/service", dependencies=[Depends(require_auth)])
async def service_status() -> dict[str, Any]:
    return service.status()


@app.post("/api/service/start", dependencies=[Depends(require_csrf)])
async def start_service() -> dict[str, Any]:
    await service.start()
    return service.status()


@app.post("/api/service/stop", dependencies=[Depends(require_csrf)])
async def stop_service() -> dict[str, Any]:
    await service.stop()
    return service.status()


@app.post("/api/service/restart", dependencies=[Depends(require_csrf)])
async def restart_service() -> dict[str, Any]:
    await service.restart()
    return service.status()


@app.get("/api/logs", dependencies=[Depends(require_auth)])
async def get_logs(limit: int = 500) -> dict[str, Any]:
    if not 1 <= limit <= 5000:
        raise HTTPException(status_code=422, detail="Limit must be between 1 and 5000")
    return {"lines": list(service.log_lines)[-limit:], "status": service.status()}


@app.delete("/api/logs", dependencies=[Depends(require_csrf)])
async def delete_logs() -> dict[str, bool]:
    try:
        service.clear_logs()
    except OSError as error:
        logger.exception("Could not delete service logs")
        raise HTTPException(status_code=500, detail="Could not delete service logs") from error
    return {"success": True}


@app.get("/api/history", dependencies=[Depends(require_auth)])
async def get_video_history(limit: int = 100, offset: int = 0, search: str = "") -> dict[str, Any]:
    if not 1 <= limit <= 500 or offset < 0:
        raise HTTPException(status_code=422, detail="Limit must be 1-500 and offset cannot be negative")
    if len(search) > 200:
        raise HTTPException(status_code=422, detail="Search must be 200 characters or fewer")
    return activity_store.history(limit, offset, search)


@app.delete("/api/history", dependencies=[Depends(require_csrf)])
async def delete_video_history() -> dict[str, bool]:
    try:
        activity_store.clear_history()
    except OSError as error:
        logger.exception("Could not delete video history")
        raise HTTPException(status_code=500, detail="Could not delete video history") from error
    return {"success": True}


@app.get("/api/stats", dependencies=[Depends(require_auth)])
async def get_statistics() -> dict[str, Any]:
    return activity_store.stats()


@app.get("/api/now-playing", dependencies=[Depends(require_auth)])
async def get_now_playing() -> dict[str, Any]:
    if not service.status()["running"]:
        return {"items": []}
    return {"items": activity_store.now_playing()}


async def _upstream_api_helper() -> tuple[aiohttp.ClientSession, api_helpers.ApiHelper]:
    settings = Config(str(DATA_DIR))
    session = aiohttp.ClientSession(trust_env=settings.use_proxy)
    return session, api_helpers.ApiHelper(settings, session)


@app.post("/api/devices/pair", dependencies=[Depends(require_csrf)])
async def pair_device(payload: PairPayload) -> dict[str, Any]:
    code = payload.pairing_code.replace("-", "").replace(" ", "")
    if not code.isdigit() or len(code) != 12:
        raise HTTPException(status_code=422, detail="Enter the 12-digit YouTube TV pairing code")
    session, api = await _upstream_api_helper()
    try:
        paired = await api.pair_with_code(code)
    except aiohttp.ClientError as error:
        raise HTTPException(status_code=502, detail=f"YouTube pairing request failed: {error}") from error
    finally:
        await session.close()
    if not paired:
        raise HTTPException(status_code=400, detail="Pairing failed; check the code and try again")
    config = load_settings()
    config.devices.append(
        DeviceConfig(
            screen_id=paired["screen_id"],
            name=payload.name.strip() or paired.get("name") or "YouTube on TV",
        )
    )
    save_config(config)
    if service.process is not None and service.process.returncode is None:
        await service.restart()
    else:
        await service.start()
    return {"device": config.devices[-1].model_dump(), "service": service.status()}


@app.post("/api/devices/discover", dependencies=[Depends(require_csrf)])
async def discover_devices() -> dict[str, Any]:
    session, api = await _upstream_api_helper()
    discovered: list[dict[str, Any]] = []
    timed_out = False
    try:
        async with asyncio.timeout(25):
            async for device in api.discover_youtube_devices_dial(active=True):
                discovered.append(device)
    except TimeoutError:
        timed_out = True
    except Exception as error:
        raise HTTPException(status_code=502, detail=f"Device discovery failed: {error}") from error
    finally:
        await session.close()
    existing_ids = {device["screen_id"] for device in load_config()["devices"]}
    return {
        "devices": [device for device in discovered if device.get("screen_id") not in existing_ids],
        "timed_out": timed_out,
    }


@app.post("/api/channels/search", dependencies=[Depends(require_csrf)])
async def search_channels(payload: ChannelSearchPayload) -> dict[str, Any]:
    session, api = await _upstream_api_helper()
    try:
        channels = await api.search_channels(payload.query.strip())
    except aiohttp.ClientError as error:
        raise HTTPException(status_code=502, detail=f"YouTube channel search failed: {error}") from error
    finally:
        await session.close()
    return {
        "channels": [{"id": channel[0], "name": channel[1], "subscribers": channel[2]} for channel in channels]
    }
