"""GeeLark cloud-phone multimodal automation framework.

The framework is intentionally app-agnostic. Configure the GeeLark ADB
connection, optional Appium endpoint, target app identifiers, and input/send
coordinates before running it against an owned or otherwise authorized app
environment.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import fcntl
import hashlib
import html
import io
import json
import logging
import math
import os
import random
import re
import shlex
import signal
import subprocess
import tempfile
import time
import unicodedata
import uuid
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Sequence

import requests

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9 fallback.
    ZoneInfo = None  # type: ignore[assignment]


LOGGER = logging.getLogger("geelark_multimodal_bot")


def _parse_wm_size_output(out: str) -> tuple[int, int] | None:
    match = re.search(r"Physical size:\s*(\d+)x(\d+)", out)
    if not match:
        match = re.search(r"Override size:\s*(\d+)x(\d+)", out)
    if not match:
        return None
    width, height = int(match.group(1)), int(match.group(2))
    if width <= 0 or height <= 0:
        return None
    return width, height


class AutomationError(Exception):
    """Base class for automation failures."""


class ADBCommandError(AutomationError):
    """Raised when an adb command fails."""


class UIExtractionError(AutomationError):
    """Raised when UI hierarchy extraction fails."""


class AIAPIError(AutomationError):
    """Raised when the multimodal AI provider returns an error."""


class GeeLarkAPIError(AutomationError):
    """Raised when the GeeLark OpenAPI returns an error."""


class DailyLimitExceeded(AutomationError):
    """Raised when the configured daily operation limit is reached."""


class PhoneRunTimeout(BaseException):
    """Raised when one cloud-phone run exceeds its configured deadline."""


class _PhoneRunDeadline:
    def __init__(self, seconds: float | None, message: str) -> None:
        self.seconds = float(seconds or 0)
        self.message = message
        self._old_handler: Any = None

    def __enter__(self) -> "_PhoneRunDeadline":
        if self.seconds <= 0:
            return self
        self._old_handler = signal.getsignal(signal.SIGALRM)
        signal.signal(signal.SIGALRM, self._handle_timeout)
        signal.setitimer(signal.ITIMER_REAL, self.seconds)
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        if self.seconds > 0:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, self._old_handler)
        return False

    def _handle_timeout(self, signum: int, frame: Any) -> None:
        raise PhoneRunTimeout(self.message)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return float(raw)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _default_adb_path() -> str:
    bundled = Path("/Users/mac/Library/Application Support/GeeLark/adb/adb")
    if bundled.exists():
        return str(bundled)
    return "adb"


def load_env_file(path: Path, *, override: bool = False) -> None:
    if not path.exists():
        raise AutomationError(f"Env file does not exist: {path}")

    loaded_keys: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        if raw.startswith("export "):
            raw = raw[7:].strip()
        key, value = raw.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if override or key not in os.environ:
            os.environ[key] = value
        loaded_keys.add(key)

    if "GEELARK_APP_ID" in loaded_keys and "GEELARK_TOKEN" not in os.environ:
        os.environ["GEELARK_TOKEN"] = os.environ["GEELARK_APP_ID"]
    os.environ.setdefault("GEELARK_AUTH_MODE", "token")

    if "MIDSCENE_OPENAI_API_KEY" in os.environ and "AI_API_KEY" not in os.environ:
        os.environ["AI_API_KEY"] = os.environ["MIDSCENE_OPENAI_API_KEY"]
    if "MIDSCENE_MODEL" in os.environ and "AI_MODEL" not in os.environ:
        os.environ["AI_MODEL"] = os.environ["MIDSCENE_MODEL"]
    if "MIDSCENE_OPENAI_BASE_URL" in os.environ and "AI_BASE_URL" not in os.environ:
        base_url = os.environ["MIDSCENE_OPENAI_BASE_URL"].rstrip("/")
        if base_url.endswith("/v1"):
            base_url = base_url[:-3].rstrip("/")
        os.environ["AI_BASE_URL"] = base_url


@dataclass(frozen=True)
class Config:
    """Centralized runtime configuration.

    The user-provided values are included as defaults to satisfy local bootstrap
    requirements. In production, override them through environment variables so
    secrets are not duplicated across scripts.
    """

    # GeeLark API credentials.
    GEELARK_API_KEY: str = field(
        default_factory=lambda: os.getenv(
            "GEELARK_API_KEY", "BVNM6CA6TXAXJA5W7IASKYT6UCPGMR"
        )
    )
    GEELARK_APP_ID: str = field(
        default_factory=lambda: os.getenv(
            "GEELARK_APP_ID", "55LF9KD93LOD5WKZEJEBYCKTYI8G6ASG"
        )
    )
    GEELARK_TOKEN: str = field(
        default_factory=lambda: os.getenv(
            "GEELARK_TOKEN", "BVNM6CA6TXAXJA5W7IASKYT6UCPGMR"
        )
    )
    GEELARK_API_BASE_URL: str = field(
        default_factory=lambda: os.getenv(
            "GEELARK_API_BASE_URL", "https://openapi.geelark.com"
        ).rstrip("/")
    )
    GEELARK_AUTH_MODE: str = field(
        default_factory=lambda: os.getenv("GEELARK_AUTH_MODE", "key").lower()
    )
    GEELARK_API_MAX_RETRIES: int = field(
        default_factory=lambda: _env_int("GEELARK_API_MAX_RETRIES", 2)
    )
    GEELARK_API_RETRY_BASE_SECONDS: float = field(
        default_factory=lambda: _env_float("GEELARK_API_RETRY_BASE_SECONDS", 0.8)
    )

    # Multimodal AI provider.
    AI_BASE_URL: str = field(
        default_factory=lambda: os.getenv(
            "AI_BASE_URL", "https://api.vectorengine.ai"
        ).rstrip("/")
    )
    AI_API_KEY: str = field(
        default_factory=lambda: os.getenv(
            "AI_API_KEY", "sk-xPS6UMovYdh0C2E1XP50hcItgTNCzFQnt2BCN7hGr3Tb8Z9M"
        )
    )
    AI_MODEL: str = field(
        default_factory=lambda: os.getenv("AI_MODEL", "gemini-3.5-flash")
    )
    AI_TIMEOUT_SECONDS: float = field(
        default_factory=lambda: _env_float("AI_TIMEOUT_SECONDS", 60.0)
    )
    AI_MAX_RETRIES: int = field(default_factory=lambda: _env_int("AI_MAX_RETRIES", 2))
    AI_REPLY_MAX_TOKENS: int = field(
        default_factory=lambda: _env_int("AI_REPLY_MAX_TOKENS", 2048)
    )

    # GeeLark ADB connection. Set either GEELARK_ADB_CONNECT_ADDRESS or
    # GEELARK_ADB_SERIAL. If a login code is provided, `glogin` is executed after
    # adb connect.
    ADB_PATH: str = field(default_factory=lambda: os.getenv("ADB_PATH", _default_adb_path()))
    GEELARK_PROFILE_ID: str = field(
        default_factory=lambda: os.getenv("GEELARK_PROFILE_ID", "")
    )
    GEELARK_ADB_CONNECT_ADDRESS: str = field(
        default_factory=lambda: os.getenv("GEELARK_ADB_CONNECT_ADDRESS", "")
    )
    GEELARK_ADB_SERIAL: str = field(
        default_factory=lambda: os.getenv("GEELARK_ADB_SERIAL", "")
    )
    GEELARK_ADB_LOGIN_CODE: str = field(
        default_factory=lambda: os.getenv("GEELARK_ADB_LOGIN_CODE", "")
    )

    # Optional Appium session, useful for page source extraction and Unicode
    # text entry.
    APPIUM_SERVER_URL: str = field(
        default_factory=lambda: os.getenv("APPIUM_SERVER_URL", "")
    )
    APPIUM_DEVICE_NAME: str = field(
        default_factory=lambda: os.getenv("APPIUM_DEVICE_NAME", "GeeLark Cloud Phone")
    )
    APPIUM_PLATFORM_VERSION: str = field(
        default_factory=lambda: os.getenv("APPIUM_PLATFORM_VERSION", "")
    )
    TARGET_PACKAGE: str = field(default_factory=lambda: os.getenv("TARGET_PACKAGE", ""))
    TARGET_ACTIVITY: str = field(
        default_factory=lambda: os.getenv("TARGET_ACTIVITY", "")
    )
    INPUT_ELEMENT_ID: str = field(
        default_factory=lambda: os.getenv("INPUT_ELEMENT_ID", "")
    )
    SEND_ELEMENT_ID: str = field(default_factory=lambda: os.getenv("SEND_ELEMENT_ID", ""))

    # Coordinate fallback for app-agnostic text entry.
    INPUT_BOX_X: int = field(default_factory=lambda: _env_int("INPUT_BOX_X", 540))
    INPUT_BOX_Y: int = field(default_factory=lambda: _env_int("INPUT_BOX_Y", 2110))
    SEND_BUTTON_X: int = field(default_factory=lambda: _env_int("SEND_BUTTON_X", 1010))
    SEND_BUTTON_Y: int = field(default_factory=lambda: _env_int("SEND_BUTTON_Y", 2110))
    DEFAULT_SCREEN_WIDTH: int = field(
        default_factory=lambda: _env_int("DEFAULT_SCREEN_WIDTH", 1080)
    )
    DEFAULT_SCREEN_HEIGHT: int = field(
        default_factory=lambda: _env_int("DEFAULT_SCREEN_HEIGHT", 2400)
    )
    SWIPE_START_X: int = field(default_factory=lambda: _env_int("SWIPE_START_X", 540))
    SWIPE_START_Y: int = field(default_factory=lambda: _env_int("SWIPE_START_Y", 1780))
    SWIPE_END_X: int = field(default_factory=lambda: _env_int("SWIPE_END_X", 540))
    SWIPE_END_Y: int = field(default_factory=lambda: _env_int("SWIPE_END_Y", 620))

    # Runtime scheduling and safety limits.
    RANDOM_WAIT_MIN_SECONDS: float = field(
        default_factory=lambda: _env_float("RANDOM_WAIT_MIN_SECONDS", 3.5)
    )
    RANDOM_WAIT_MAX_SECONDS: float = field(
        default_factory=lambda: _env_float("RANDOM_WAIT_MAX_SECONDS", 8.0)
    )
    PRE_LIKE_WAIT_MIN_SECONDS: float = field(
        default_factory=lambda: _env_float("PRE_LIKE_WAIT_MIN_SECONDS", 0.45)
    )
    PRE_LIKE_WAIT_MAX_SECONDS: float = field(
        default_factory=lambda: _env_float("PRE_LIKE_WAIT_MAX_SECONDS", 1.7)
    )
    DAILY_ACTION_LIMIT: int = field(
        default_factory=lambda: _env_int("DAILY_ACTION_LIMIT", 80)
    )
    POPUP_CHECK_EVERY_N_ACTIONS: int = field(
        default_factory=lambda: _env_int("POPUP_CHECK_EVERY_N_ACTIONS", 8)
    )
    SELF_PROFILE_SCROLLS: int = field(
        default_factory=lambda: _env_int("SELF_PROFILE_SCROLLS", 6)
    )
    STOP_PHONE_AFTER_GROUP_RUN: bool = field(
        default_factory=lambda: _env_bool("STOP_PHONE_AFTER_GROUP_RUN", True)
    )
    BUMBLE_OPEN_WAIT_MIN_SECONDS: float = field(
        default_factory=lambda: _env_float("BUMBLE_OPEN_WAIT_MIN_SECONDS", 2.2)
    )
    BUMBLE_OPEN_WAIT_MAX_SECONDS: float = field(
        default_factory=lambda: _env_float("BUMBLE_OPEN_WAIT_MAX_SECONDS", 4.0)
    )
    BUMBLE_TAB_WAIT_MIN_SECONDS: float = field(
        default_factory=lambda: _env_float("BUMBLE_TAB_WAIT_MIN_SECONDS", 2.4)
    )
    BUMBLE_TAB_WAIT_MAX_SECONDS: float = field(
        default_factory=lambda: _env_float("BUMBLE_TAB_WAIT_MAX_SECONDS", 4.2)
    )
    BUMBLE_MAX_REPLY_PARTS: int = field(
        default_factory=lambda: _env_int("BUMBLE_MAX_REPLY_PARTS", 1)
    )
    LOOP_FOREVER: bool = field(default_factory=lambda: _env_bool("LOOP_FOREVER", True))
    STATE_FILE: Path = field(
        default_factory=lambda: Path(
            os.getenv(
                "STATE_FILE",
                str(Path.home() / ".geelark_multimodal_bot_state.json"),
            )
        )
    )
    HISTORY_FILE: Path = field(
        default_factory=lambda: Path(
            os.getenv(
                "HISTORY_FILE",
                str(Path.home() / ".geelark_multimodal_chat_history.json"),
            )
        )
    )
    HISTORY_MAX_TURNS: int = field(
        default_factory=lambda: _env_int("HISTORY_MAX_TURNS", 30)
    )

    # ADBKeyboard can input Unicode if installed and selected on the device.
    USE_ADB_KEYBOARD: bool = field(
        default_factory=lambda: _env_bool("USE_ADB_KEYBOARD", False)
    )
    AUTO_INSTALL_ADB_KEYBOARD: bool = field(
        default_factory=lambda: _env_bool("AUTO_INSTALL_ADB_KEYBOARD", True)
    )
    ADB_KEYBOARD_IME: str = field(
        default_factory=lambda: os.getenv(
            "ADB_KEYBOARD_IME", "com.android.adbkeyboard/.AdbIME"
        )
    )
    ADB_KEYBOARD_APK_PATH: Path = field(
        default_factory=lambda: Path(
            os.getenv("ADB_KEYBOARD_APK_PATH", str(Path.cwd() / "tools" / "ADBKeyboard.apk"))
        )
    )
    AUTO_START_PHONE: bool = field(
        default_factory=lambda: _env_bool("AUTO_START_PHONE", False)
    )
    AUTO_ENABLE_ADB: bool = field(
        default_factory=lambda: _env_bool("AUTO_ENABLE_ADB", False)
    )
    GEELARK_PHONE_START_TIMEOUT_SECONDS: int = field(
        default_factory=lambda: _env_int("GEELARK_PHONE_START_TIMEOUT_SECONDS", 240)
    )
    RIGHT_SWIPE_START_X: int = field(
        default_factory=lambda: _env_int("RIGHT_SWIPE_START_X", 260)
    )
    RIGHT_SWIPE_START_Y: int = field(
        default_factory=lambda: _env_int("RIGHT_SWIPE_START_Y", 1350)
    )
    RIGHT_SWIPE_END_X: int = field(
        default_factory=lambda: _env_int("RIGHT_SWIPE_END_X", 930)
    )
    RIGHT_SWIPE_END_Y: int = field(
        default_factory=lambda: _env_int("RIGHT_SWIPE_END_Y", 1280)
    )
    RIGHT_SWIPE_JITTER_X: int = field(
        default_factory=lambda: _env_int("RIGHT_SWIPE_JITTER_X", 80)
    )
    RIGHT_SWIPE_JITTER_Y: int = field(
        default_factory=lambda: _env_int("RIGHT_SWIPE_JITTER_Y", 120)
    )
    PROFILE_VIEW_PROBABILITY: float = field(
        default_factory=lambda: _env_float("PROFILE_VIEW_PROBABILITY", 0.72)
    )
    PROFILE_VIEW_MAX_SWIPES: int = field(
        default_factory=lambda: _env_int("PROFILE_VIEW_MAX_SWIPES", 2)
    )
    BUMBLE_LIKE_VERIFY_ATTEMPTS: int = field(
        default_factory=lambda: _env_int("BUMBLE_LIKE_VERIFY_ATTEMPTS", 3)
    )
    BUMBLE_LIKE_VERIFY_WAIT_MIN_SECONDS: float = field(
        default_factory=lambda: _env_float("BUMBLE_LIKE_VERIFY_WAIT_MIN_SECONDS", 0.35)
    )
    BUMBLE_LIKE_VERIFY_WAIT_MAX_SECONDS: float = field(
        default_factory=lambda: _env_float("BUMBLE_LIKE_VERIFY_WAIT_MAX_SECONDS", 0.75)
    )
    PROFILE_VIEW_START_X: int = field(
        default_factory=lambda: _env_int("PROFILE_VIEW_START_X", 540)
    )
    PROFILE_VIEW_START_Y: int = field(
        default_factory=lambda: _env_int("PROFILE_VIEW_START_Y", 1760)
    )
    PROFILE_VIEW_END_X: int = field(
        default_factory=lambda: _env_int("PROFILE_VIEW_END_X", 540)
    )
    PROFILE_VIEW_END_Y: int = field(
        default_factory=lambda: _env_int("PROFILE_VIEW_END_Y", 760)
    )
    PROFILE_VIEW_JITTER_X: int = field(
        default_factory=lambda: _env_int("PROFILE_VIEW_JITTER_X", 85)
    )
    PROFILE_VIEW_JITTER_Y: int = field(
        default_factory=lambda: _env_int("PROFILE_VIEW_JITTER_Y", 145)
    )
    CUSTOMER_PHOTO_CROP_BOUNDS: str = field(
        default_factory=lambda: os.getenv(
            "CUSTOMER_PHOTO_CROP_BOUNDS", "20,430,1060,2050"
        )
    )


@dataclass
class TextNode:
    text: str
    class_name: str
    resource_id: str = ""
    content_desc: str = ""
    bounds: str = ""


@dataclass
class PageContext:
    activity: str
    text_views: list[TextNode]
    inferred: dict[str, str]
    captured_at: str

    def compact_text(self) -> str:
        lines = [f"Activity: {self.activity or 'unknown'}"]
        if self.inferred:
            lines.append("Inferred: " + json.dumps(self.inferred, ensure_ascii=False))
        lines.extend(node.text for node in self.text_views if node.text)
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "activity": self.activity,
            "captured_at": self.captured_at,
            "inferred": self.inferred,
            "text_views": [asdict(node) for node in self.text_views],
        }


class GeelarkOpenAPIClient:
    """Small helper for GeeLark OpenAPI calls.

    GeeLark currently documents that API calls are JSON POST requests and that
    ADB enablement is asynchronous. Endpoint paths vary by account feature, so
    keep the path explicit at call sites.
    """

    def __init__(self, config: Config) -> None:
        self.config = config

    def post(
        self, path: str, payload: dict[str, Any], timeout: float = 30.0
    ) -> dict[str, Any]:
        url = f"{self.config.GEELARK_API_BASE_URL}/{path.lstrip('/')}"
        body = dict(payload)
        last_error: Exception | None = None
        for attempt in range(self.config.GEELARK_API_MAX_RETRIES + 1):
            headers = self._headers()
            old_alarm_handler = None
            old_alarm_timer: tuple[float, float] | None = None
            alarm_seconds = max(1.0, float(timeout) + 3.0)

            def _raise_wall_clock_timeout(signum: int, frame: Any) -> None:
                del signum, frame
                raise requests.Timeout(f"GeeLark API wall-clock timeout after {alarm_seconds:.1f}s")

            try:
                if hasattr(signal, "SIGALRM"):
                    old_alarm_handler = signal.getsignal(signal.SIGALRM)
                    old_alarm_timer = signal.setitimer(signal.ITIMER_REAL, 0)
                    signal.signal(signal.SIGALRM, _raise_wall_clock_timeout)
                    signal.setitimer(signal.ITIMER_REAL, alarm_seconds)
                response = requests.post(url, headers=headers, json=body, timeout=timeout)
                response.raise_for_status()
                data = response.json()
                if int(data.get("code", 0)) != 0:
                    raise GeeLarkAPIError(
                        f"GeeLark API returned code={data.get('code')} msg={data.get('msg')}"
                    )
                return data
            except requests.Timeout as exc:
                last_error = exc
                error_text = f"GeeLark API timeout: {url}"
            except requests.RequestException as exc:
                last_error = exc
                error_text = f"GeeLark API request failed: {url}: {exc}"
            except ValueError as exc:
                last_error = exc
                error_text = f"GeeLark API returned non-JSON response: {url}"
            except GeeLarkAPIError as exc:
                last_error = exc
                error_text = str(exc)
            finally:
                if old_alarm_handler is not None:
                    signal.setitimer(signal.ITIMER_REAL, 0)
                    signal.signal(signal.SIGALRM, old_alarm_handler)
                    if old_alarm_timer and old_alarm_timer[0] > 0:
                        signal.setitimer(
                            signal.ITIMER_REAL,
                            old_alarm_timer[0],
                            old_alarm_timer[1],
                        )

            if attempt >= self.config.GEELARK_API_MAX_RETRIES:
                raise GeeLarkAPIError(error_text) from last_error

            sleep_s = (
                self.config.GEELARK_API_RETRY_BASE_SECONDS * (attempt + 1)
                + random.uniform(0.15, 0.55)
            )
            LOGGER.warning(
                "GeeLark API call failed on attempt %s/%s; retrying in %.2fs: %s",
                attempt + 1,
                self.config.GEELARK_API_MAX_RETRIES + 1,
                sleep_s,
                error_text,
            )
            time.sleep(sleep_s)

        raise GeeLarkAPIError(f"GeeLark API request failed: {url}") from last_error

    def phone_status(self, ids: list[str]) -> dict[str, Any]:
        return self.post("/open/v1/phone/status", {"ids": ids})

    def start_phone(self, ids: list[str]) -> dict[str, Any]:
        return self.post("/open/v1/phone/start", {"ids": ids})

    def stop_phone(self, ids: list[str]) -> dict[str, Any]:
        return self.post("/open/v1/phone/stop", {"ids": ids})

    def phone_list(self, page: int = 1, page_size: int = 100) -> dict[str, Any]:
        return self.post(
            "/open/v1/phone/list",
            {"page": int(page), "pageSize": int(page_size)},
        )

    def group_list(self, page: int = 1, page_size: int = 100) -> dict[str, Any]:
        return self.post(
            "/open/v1/group/list",
            {"page": int(page), "pageSize": int(page_size)},
        )

    def set_adb_status(self, ids: list[str], open_: bool = True) -> dict[str, Any]:
        return self.post("/open/v1/adb/setStatus", {"ids": ids, "open": open_})

    def get_adb_info(self, ids: list[str]) -> dict[str, Any]:
        return self.post("/open/v1/adb/getData", {"ids": ids})

    def execute_shell(self, phone_id: str, cmd: str, *, timeout: float = 15.0) -> dict[str, Any]:
        return self.post("/open/v1/shell/execute", {"id": phone_id, "cmd": cmd}, timeout=timeout)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        trace_id = str(uuid.uuid4())
        headers["traceId"] = trace_id
        if self.config.GEELARK_AUTH_MODE == "token":
            headers["Authorization"] = f"Bearer {self.config.GEELARK_TOKEN}"
            return headers

        ts = str(int(time.time() * 1000))
        nonce = trace_id[:6]
        sign_src = (
            self.config.GEELARK_APP_ID
            + trace_id
            + ts
            + nonce
            + self.config.GEELARK_API_KEY
        )
        headers.update(
            {
                "appId": self.config.GEELARK_APP_ID,
                "ts": ts,
                "nonce": nonce,
                "sign": hashlib.sha256(sign_src.encode("utf-8")).hexdigest().upper(),
            }
        )
        return headers


def _extract_phone_status(payload: dict[str, Any], phone_id: str) -> int | None:
    data = payload.get("data") or {}
    for item in data.get("successDetails") or []:
        if str(item.get("id")) == str(phone_id):
            return int(item.get("status"))
    for item in data.get("failDetails") or []:
        if str(item.get("id")) == str(phone_id):
            return int(item.get("code"))
    return None


def _extract_adb_info_item(payload: dict[str, Any], phone_id: str) -> dict[str, Any]:
    data = payload.get("data") or {}
    for item in data.get("items") or []:
        if str(item.get("id")) == str(phone_id):
            return dict(item)
    raise ADBCommandError(f"ADB info for {phone_id} was not present in response.")


def _is_transient_adb_connection_error(message: str) -> bool:
    lowered = message.lower()
    markers = (
        "device offline",
        "device not found",
        "no devices/emulators found",
        "closed",
        "connection reset",
        "failed to connect",
        "unable to connect",
        "adb command failed (255)",
        "you should run glogin to login first",
    )
    return any(marker in lowered for marker in markers)


def _adb_output_requires_relogin(message: str) -> bool:
    lowered = str(message or "").lower()
    return "you should run glogin to login first" in lowered


class GeelarkADBDevice:
    """ADB-backed GeeLark cloud phone controller."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.serial = config.GEELARK_ADB_SERIAL or config.GEELARK_ADB_CONNECT_ADDRESS
        self.openapi = GeelarkOpenAPIClient(config)
        self._screen_size_cache: tuple[int, int] | None = None

    def ensure_connected(self) -> None:
        if not self.config.GEELARK_ADB_CONNECT_ADDRESS and not self.serial:
            self._resolve_adb_connection_from_profile()

        if self.config.GEELARK_ADB_CONNECT_ADDRESS:
            cmd = [self.config.ADB_PATH, "connect", self.config.GEELARK_ADB_CONNECT_ADDRESS]
            self._run_raw(cmd, timeout=20)
            self.serial = self.config.GEELARK_ADB_CONNECT_ADDRESS

        if not self.serial:
            raise ADBCommandError(
                "Missing GEELARK_ADB_CONNECT_ADDRESS or GEELARK_ADB_SERIAL."
            )

        last_error: Exception | None = None
        for attempt in range(5):
            try:
                state = str(self.adb(["get-state"], timeout=10, check=False)).strip()
                if state != "device":
                    raise ADBCommandError(f"ADB device is not ready, current state: {state!r}")
                if self.config.GEELARK_ADB_LOGIN_CODE:
                    self.shell("glogin", self.config.GEELARK_ADB_LOGIN_CODE, timeout=20)
                state = str(self.adb(["get-state"], timeout=10, check=False)).strip()
                if state == "device":
                    return
                raise ADBCommandError(f"ADB device is not ready after login, current state: {state!r}")
            except ADBCommandError as exc:
                last_error = exc
                if attempt >= 4:
                    break
                LOGGER.warning(
                    "ADB connection for %s not ready on attempt %s/5: %s",
                    self.serial,
                    attempt + 1,
                    exc,
                )
                if self.config.GEELARK_PROFILE_ID:
                    LOGGER.info(
                        "Refreshing GeeLark ADB endpoint/password for profile %s after failed local ADB attempt.",
                        self.config.GEELARK_PROFILE_ID,
                    )
                    if attempt >= 1:
                        self._reset_geelark_adb_tunnel()
                    self._recover_adb_connection()
                elif self.config.GEELARK_ADB_CONNECT_ADDRESS:
                    self._run_raw(
                        [self.config.ADB_PATH, "disconnect", self.config.GEELARK_ADB_CONNECT_ADDRESS],
                        timeout=10,
                        check=False,
                    )
                    time.sleep(1.2 + attempt * 0.8)
                    self._run_raw(
                        [self.config.ADB_PATH, "connect", self.config.GEELARK_ADB_CONNECT_ADDRESS],
                        timeout=20,
                        check=False,
                    )
                time.sleep(0.8 + attempt * 0.5)
        raise ADBCommandError(f"ADB device did not become ready: {last_error}")

    def _reset_geelark_adb_tunnel(self) -> None:
        phone_id = self.config.GEELARK_PROFILE_ID
        if not phone_id:
            return
        old_address = self.config.GEELARK_ADB_CONNECT_ADDRESS
        LOGGER.info("Resetting GeeLark ADB tunnel for profile %s.", phone_id)
        try:
            if old_address:
                self._run_raw(
                    [self.config.ADB_PATH, "disconnect", old_address],
                    timeout=10,
                    check=False,
                )
            self.openapi.set_adb_status([phone_id], open_=False)
            time.sleep(2.5)
            self.openapi.set_adb_status([phone_id], open_=True)
            time.sleep(5.0)
        except Exception as exc:
            LOGGER.warning("GeeLark ADB tunnel reset failed for %s: %s", phone_id, exc)

    def _resolve_adb_connection_from_profile(self) -> None:
        phone_id = self.config.GEELARK_PROFILE_ID
        if not phone_id:
            return

        status_payload = self.openapi.phone_status([phone_id])
        status = _extract_phone_status(status_payload, phone_id)
        if status != 0:
            if not self.config.AUTO_START_PHONE:
                raise ADBCommandError(
                    f"Cloud phone {phone_id} is not started (status={status}). "
                    "Set AUTO_START_PHONE=1 or run the test command with --prepare-adb."
                )
            self.openapi.start_phone([phone_id])
            self._wait_for_phone_started(
                phone_id,
                timeout_s=self.config.GEELARK_PHONE_START_TIMEOUT_SECONDS,
            )

        if self.config.AUTO_ENABLE_ADB:
            self.openapi.set_adb_status([phone_id], open_=True)
            time.sleep(3.2)

        adb_info = self.openapi.get_adb_info([phone_id])
        item = _extract_adb_info_item(adb_info, phone_id)
        if int(item.get("code", 0)) != 0:
            raise ADBCommandError(
                f"Could not get ADB info for {phone_id}: code={item.get('code')}"
            )
        ip = str(item.get("ip") or "")
        port = str(item.get("port") or "")
        pwd = str(item.get("pwd") or "")
        if not ip or not port:
            raise ADBCommandError(f"ADB info for {phone_id} did not include ip/port.")

        object.__setattr__(self.config, "GEELARK_ADB_LOGIN_CODE", pwd)
        object.__setattr__(self.config, "GEELARK_ADB_CONNECT_ADDRESS", f"{ip}:{port}")
        self.serial = self.config.GEELARK_ADB_CONNECT_ADDRESS

    def _wait_for_phone_started(self, phone_id: str, timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            status_payload = self.openapi.phone_status([phone_id])
            status = _extract_phone_status(status_payload, phone_id)
            if status == 0:
                return
            time.sleep(3.0)
        raise ADBCommandError(f"Cloud phone {phone_id} did not start within {timeout_s}s.")

    def adb(
        self,
        args: list[str],
        *,
        timeout: float = 30.0,
        text: bool = True,
        check: bool = True,
    ) -> str | bytes:
        if not self.serial:
            raise ADBCommandError("ADB serial is not initialized.")
        cmd = [self.config.ADB_PATH, "-s", self.serial, *args]
        try:
            return self._run_raw(cmd, timeout=timeout, text=text, check=check)
        except ADBCommandError as exc:
            if not _is_transient_adb_connection_error(str(exc)):
                raise
            LOGGER.warning("ADB command hit transient connection error; reconnecting %s.", self.serial)
            self._recover_adb_connection()
            return self._run_raw(cmd, timeout=timeout, text=text, check=check)

    def shell(self, *args: Any, timeout: float = 30.0, check: bool = True) -> str:
        out = self.adb(
            ["shell", *[str(arg) for arg in args]],
            timeout=timeout,
            text=True,
            check=check,
        )
        return str(out)

    def exec_out(self, *args: Any, timeout: float = 30.0) -> bytes:
        out = self.adb(
            ["exec-out", *[str(arg) for arg in args]],
            timeout=timeout,
            text=False,
            check=True,
        )
        return bytes(out)

    def tap(self, x: int, y: int) -> None:
        self.shell("sh", "-c", f"input tap {int(x)} {int(y)}; true", timeout=8)

    def motion_event(self, action: str, x: int, y: int) -> None:
        self.shell(
            "sh",
            "-c",
            f"input motionevent {shlex.quote(action.upper())} {int(x)} {int(y)}; true",
            timeout=8,
        )

    def swipe_segment(
        self, start: tuple[int, int], end: tuple[int, int], duration_ms: int
    ) -> None:
        self.shell(
            "sh",
            "-c",
            (
                f"input swipe {int(start[0])} {int(start[1])} "
                f"{int(end[0])} {int(end[1])} {max(1, int(duration_ms))}; true"
            ),
            timeout=8,
        )

    def get_screen_size(self) -> tuple[int, int]:
        last_output = ""
        for attempt in range(3):
            try:
                out = self.shell("wm", "size", timeout=8, check=False)
            except ADBCommandError as exc:
                last_output = str(exc)
            else:
                last_output = out
                parsed = _parse_wm_size_output(out)
                if parsed is not None:
                    self._screen_size_cache = parsed
                    return parsed
            time.sleep(0.25 + attempt * 0.25)
        if self._screen_size_cache is not None:
            LOGGER.warning("Using cached screen size after wm size failed: %s", last_output)
            return self._screen_size_cache
        fallback = (int(self.config.DEFAULT_SCREEN_WIDTH), int(self.config.DEFAULT_SCREEN_HEIGHT))
        LOGGER.warning("Using default screen size %s after wm size failed: %s", fallback, last_output)
        self._screen_size_cache = fallback
        return fallback

    def get_current_activity(self) -> str:
        last_relogin_required = False
        for attempt in range(2):
            relogin_required = False
            for command in (
                ("dumpsys", "window", "windows"),
                ("dumpsys", "activity", "activities"),
            ):
                out = self.shell(*command, timeout=10, check=False)
                if _adb_output_requires_relogin(out):
                    LOGGER.warning(
                        "ADB shell requires glogin while reading activity; reconnecting %s.",
                        self.serial,
                    )
                    self._recover_adb_connection()
                    relogin_required = True
                    last_relogin_required = True
                    break
                for pattern in (
                    r"mCurrentFocus=.*?\s([A-Za-z0-9_.$]+/[A-Za-z0-9_.$]+)",
                    r"topResumedActivity=.*?\s([A-Za-z0-9_.$]+/[A-Za-z0-9_.$]+)",
                    r"mResumedActivity=.*?\s([A-Za-z0-9_.$]+/[A-Za-z0-9_.$]+)",
                ):
                    match = re.search(pattern, out)
                    if match:
                        return match.group(1)
            if not relogin_required:
                break
            time.sleep(0.7 + attempt * 0.5)
        if last_relogin_required:
            raise ADBCommandError("ADB shell still requires glogin after reconnect attempts.")
        return ""

    def dump_ui_xml(self) -> str:
        dump_path = "/sdcard/window_dump.xml"
        self.shell("uiautomator", "dump", dump_path, timeout=15)
        xml_bytes = self.exec_out("cat", dump_path, timeout=15)
        xml_text = xml_bytes.decode("utf-8", errors="replace").strip()
        if not xml_text.startswith("<?xml") and "<hierarchy" not in xml_text:
            raise UIExtractionError("ADB uiautomator dump did not return XML.")
        return xml_text

    def screenshot_png(self) -> bytes:
        data = self.exec_out("screencap", "-p", timeout=20)
        if not data.startswith(b"\x89PNG"):
            # Some adb builds return CRLF-corrupted PNG data.
            data = data.replace(b"\r\n", b"\n")
        if not data.startswith(b"\x89PNG"):
            raise ADBCommandError("ADB screencap did not return PNG data.")
        return data

    def input_text(self, text: str) -> None:
        if self.config.USE_ADB_KEYBOARD:
            encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
            self.shell(
                "am",
                "broadcast",
                "-a",
                "ADB_INPUT_B64",
                "--es",
                "msg",
                encoded,
                timeout=10,
            )
            return

        if any(ord(ch) > 127 for ch in text):
            if self.config.AUTO_INSTALL_ADB_KEYBOARD:
                _ensure_adb_keyboard_enabled(self, self.config)
                encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
                self.shell(
                    "am",
                    "broadcast",
                    "-a",
                    "ADB_INPUT_B64",
                    "--es",
                    "msg",
                    encoded,
                    timeout=10,
                )
                return
            raise ADBCommandError(
                "Plain adb input text cannot reliably enter non-ASCII text. "
                "Use Appium, or install/select ADBKeyboard and set USE_ADB_KEYBOARD=1."
            )

        escaped = _escape_adb_input_text(text)
        self.shell("input", "text", escaped, timeout=10)

    def _recover_adb_connection(self) -> None:
        old_address = self.config.GEELARK_ADB_CONNECT_ADDRESS
        if self.config.GEELARK_PROFILE_ID:
            try:
                self._resolve_adb_connection_from_profile()
                if (
                    old_address
                    and self.config.GEELARK_ADB_CONNECT_ADDRESS
                    and old_address != self.config.GEELARK_ADB_CONNECT_ADDRESS
                ):
                    self._run_raw(
                        [self.config.ADB_PATH, "disconnect", old_address],
                        timeout=10,
                        check=False,
                    )
            except Exception as exc:
                LOGGER.warning("Could not refresh GeeLark ADB info during reconnect: %s", exc)

        if not self.config.GEELARK_ADB_CONNECT_ADDRESS:
            time.sleep(1.5)
            return
        self._run_raw(
            [self.config.ADB_PATH, "disconnect", self.config.GEELARK_ADB_CONNECT_ADDRESS],
            timeout=10,
            check=False,
        )
        time.sleep(1.5)
        self._run_raw(
            [self.config.ADB_PATH, "connect", self.config.GEELARK_ADB_CONNECT_ADDRESS],
            timeout=20,
            check=False,
        )
        time.sleep(1.5)
        if self.config.GEELARK_ADB_LOGIN_CODE:
            self._run_raw(
                [
                    self.config.ADB_PATH,
                    "-s",
                    self.config.GEELARK_ADB_CONNECT_ADDRESS,
                    "shell",
                    "glogin",
                    self.config.GEELARK_ADB_LOGIN_CODE,
                ],
                timeout=20,
                check=False,
            )
            time.sleep(0.8)

    @staticmethod
    def _run_raw(
        cmd: list[str],
        *,
        timeout: float,
        text: bool = True,
        check: bool = True,
    ) -> str | bytes:
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                check=False,
                timeout=timeout,
                text=text,
            )
        except FileNotFoundError as exc:
            raise ADBCommandError(f"ADB binary not found: {cmd[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ADBCommandError(f"ADB command timed out: {' '.join(cmd)}") from exc

        stdout = proc.stdout
        stderr = proc.stderr
        if check and proc.returncode != 0:
            if isinstance(stderr, bytes):
                stderr_text = stderr.decode("utf-8", errors="replace")
            else:
                stderr_text = stderr
            raise ADBCommandError(
                f"ADB command failed ({proc.returncode}): {' '.join(cmd)}: "
                f"{stderr_text.strip()}"
            )
        return stdout


class GeelarkOpenAPIShellDevice:
    """GeeLark shell-execute fallback for hosts without a local adb binary."""

    def __init__(self, config: Config) -> None:
        if not config.GEELARK_PROFILE_ID:
            raise ADBCommandError("GEELARK_PROFILE_ID is required for OpenAPI shell mode.")
        self.config = config
        self.serial = config.GEELARK_PROFILE_ID
        self.openapi = GeelarkOpenAPIClient(config)
        self._screen_size_cache: tuple[int, int] | None = None

    def ensure_connected(self) -> None:
        phone_id = self.config.GEELARK_PROFILE_ID
        LOGGER.info("Checking cloud phone %s status via GeeLark OpenAPI shell.", phone_id)
        status_payload = self.openapi.phone_status([phone_id])
        status = _extract_phone_status(status_payload, phone_id)
        LOGGER.info("Cloud phone %s initial status=%s.", phone_id, status)
        if status != 0:
            if not self.config.AUTO_START_PHONE:
                raise ADBCommandError(
                    f"Cloud phone {phone_id} is not started "
                    f"(status={status}). Use --prepare-adb to start it."
                )
            LOGGER.info("Starting cloud phone %s.", phone_id)
            self.openapi.start_phone([phone_id])
            deadline = time.monotonic() + self.config.GEELARK_PHONE_START_TIMEOUT_SECONDS
            last_status_log = 0.0
            while time.monotonic() < deadline:
                status_payload = self.openapi.phone_status([phone_id])
                status = _extract_phone_status(
                    status_payload, phone_id
                )
                if status == 0:
                    LOGGER.info("Cloud phone %s is running.", phone_id)
                    return
                now = time.monotonic()
                if now - last_status_log >= 12:
                    LOGGER.info("Waiting for cloud phone %s to start; status=%s.", phone_id, status)
                    last_status_log = now
                time.sleep(3)
            final_payload = self.openapi.phone_status([phone_id])
            final_status = _extract_phone_status(final_payload, phone_id)
            if final_status == 0:
                LOGGER.info("Cloud phone %s is running after final status check.", phone_id)
                return
            raise ADBCommandError(
                f"Cloud phone did not start within {self.config.GEELARK_PHONE_START_TIMEOUT_SECONDS}s."
            )
        LOGGER.info("Cloud phone %s is already running.", phone_id)

    def shell(self, *args: Any, timeout: float = 30.0, check: bool = True) -> str:
        cmd = " ".join(shlex.quote(str(arg)) for arg in args)
        return self._execute(cmd, check=check, timeout=timeout)

    def _execute(self, cmd: str, check: bool = True, timeout: float = 15.0) -> str:
        payload = self.openapi.execute_shell(
            self.config.GEELARK_PROFILE_ID,
            cmd,
            timeout=timeout,
        )
        data = payload.get("data") or {}
        status = bool(data.get("status"))
        output = str(data.get("output") or "")
        if check and not status:
            raise ADBCommandError(f"OpenAPI shell command failed: {cmd}: {output}")
        return output

    def tap(self, x: int, y: int) -> None:
        self.shell("input", "tap", int(x), int(y), timeout=8)

    def motion_event(self, action: str, x: int, y: int) -> None:
        self.shell("input", "motionevent", action.upper(), int(x), int(y), timeout=8)

    def swipe_segment(
        self, start: tuple[int, int], end: tuple[int, int], duration_ms: int
    ) -> None:
        self.shell(
            "input",
            "swipe",
            int(start[0]),
            int(start[1]),
            int(end[0]),
            int(end[1]),
            max(1, int(duration_ms)),
            timeout=8,
        )

    def get_screen_size(self) -> tuple[int, int]:
        last_output = ""
        for attempt in range(3):
            try:
                out = self.shell("wm", "size", timeout=8, check=False)
            except ADBCommandError as exc:
                last_output = str(exc)
            else:
                last_output = out
                parsed = _parse_wm_size_output(out)
                if parsed is not None:
                    self._screen_size_cache = parsed
                    return parsed
            time.sleep(0.25 + attempt * 0.25)
        if self._screen_size_cache is not None:
            LOGGER.warning("Using cached screen size after wm size failed: %s", last_output)
            return self._screen_size_cache
        fallback = (int(self.config.DEFAULT_SCREEN_WIDTH), int(self.config.DEFAULT_SCREEN_HEIGHT))
        LOGGER.warning("Using default screen size %s after wm size failed: %s", fallback, last_output)
        self._screen_size_cache = fallback
        return fallback

    def get_current_activity(self) -> str:
        for command in (
            ("dumpsys", "window", "windows"),
            ("dumpsys", "activity", "activities"),
        ):
            out = self.shell(*command, timeout=10, check=False)
            for pattern in (
                r"mCurrentFocus=.*?\s([A-Za-z0-9_.$]+/[A-Za-z0-9_.$]+)",
                r"topResumedActivity=.*?\s([A-Za-z0-9_.$]+/[A-Za-z0-9_.$]+)",
                r"mResumedActivity=.*?\s([A-Za-z0-9_.$]+/[A-Za-z0-9_.$]+)",
            ):
                match = re.search(pattern, out)
                if match:
                    return match.group(1)
        return ""

    def dump_ui_xml(self) -> str:
        dump_path = "/sdcard/window_dump.xml"
        last_text = ""
        for attempt in range(3):
            self._execute(f"uiautomator dump {dump_path}", check=False)
            xml_bytes = self._read_remote_file_bytes(dump_path)
            xml_text = xml_bytes.decode("utf-8", errors="replace").strip()
            start = xml_text.find("<?xml")
            if start < 0:
                start = xml_text.find("<hierarchy")
            if start >= 0:
                xml_text = xml_text[start:]
            last_text = xml_text
            if xml_text.startswith("<?xml") or "<hierarchy" in xml_text:
                return xml_text
            time.sleep(0.8 + attempt * 0.6)
        raise UIExtractionError(
            "OpenAPI shell uiautomator dump did not return XML. "
            f"Last output prefix: {last_text[:120]!r}"
        )

    def screenshot_png(self) -> bytes:
        remote_path = f"/sdcard/geelark_screen_{uuid.uuid4().hex}.png"
        try:
            self._execute(f"screencap -p {remote_path}", check=False)
            data = self._read_remote_file_bytes(remote_path)
        finally:
            self._execute(f"rm -f {remote_path}", check=False)
        if not data.startswith(b"\x89PNG"):
            data = data.replace(b"\r\n", b"\n")
        if not data.startswith(b"\x89PNG"):
            raise ADBCommandError("OpenAPI shell screencap did not return PNG data.")
        return data

    def _read_remote_file_bytes(self, remote_path: str, chunk_size: int = 1800) -> bytes:
        b64_path = f"/sdcard/geelark_tmp_{uuid.uuid4().hex}.b64"
        try:
            self._execute(
                f"base64 {shlex.quote(remote_path)} | tr -d '\\n' > {b64_path}",
                check=False,
            )
            size_out = self._execute(f"wc -c {b64_path}", check=False)
            match = re.search(r"(\d+)", size_out)
            if not match:
                raise ADBCommandError(f"Could not determine remote file size: {remote_path}")
            b64_len = int(match.group(1))
            chunks: list[str] = []
            for skip in range(0, b64_len, chunk_size):
                chunk = self._execute(
                    f"dd if={b64_path} bs={chunk_size} skip={skip // chunk_size} count=1 2>/dev/null",
                    check=False,
                )
                chunks.append("".join(chunk.split()))
            return base64.b64decode("".join(chunks), validate=False)
        except ValueError as exc:
            raise ADBCommandError(f"Remote file was not valid Base64: {remote_path}") from exc
        finally:
            self._execute(f"rm -f {b64_path}", check=False)

    def input_text(self, text: str) -> None:
        if self.config.USE_ADB_KEYBOARD:
            encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
            self.shell(
                "am",
                "broadcast",
                "-a",
                "ADB_INPUT_B64",
                "--es",
                "msg",
                encoded,
                timeout=10,
            )
            return
        if any(ord(ch) > 127 for ch in text):
            if self.config.AUTO_INSTALL_ADB_KEYBOARD:
                _ensure_adb_keyboard_enabled(self, self.config)
                encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
                self.shell(
                    "am",
                    "broadcast",
                    "-a",
                    "ADB_INPUT_B64",
                    "--es",
                    "msg",
                    encoded,
                    timeout=10,
                )
                return
            raise ADBCommandError(
                "OpenAPI shell text fallback needs Appium or ADBKeyboard for non-ASCII."
            )
        self.shell("input", "text", _escape_adb_input_text(text), timeout=10)


class AppiumController:
    """Optional Appium layer for XML extraction and Unicode text entry."""

    def __init__(self, config: Config, adb_device: GeelarkADBDevice) -> None:
        self.config = config
        self.adb_device = adb_device
        self.driver: Any | None = None

    def connect(self) -> None:
        if not self.config.APPIUM_SERVER_URL:
            return

        try:
            from appium import webdriver
            from appium.options.android import UiAutomator2Options
        except ImportError as exc:
            raise AutomationError(
                "Appium-Python-Client is not installed. Run pip install -r requirements.txt."
            ) from exc

        options = UiAutomator2Options()
        options.platform_name = "Android"
        options.automation_name = "UiAutomator2"
        options.device_name = self.config.APPIUM_DEVICE_NAME
        options.udid = self.adb_device.serial
        options.set_capability("noReset", True)
        options.set_capability("newCommandTimeout", 300)
        if self.config.APPIUM_PLATFORM_VERSION:
            options.platform_version = self.config.APPIUM_PLATFORM_VERSION
        if self.config.TARGET_PACKAGE:
            options.app_package = self.config.TARGET_PACKAGE
        if self.config.TARGET_ACTIVITY:
            options.app_activity = self.config.TARGET_ACTIVITY

        self.driver = webdriver.Remote(self.config.APPIUM_SERVER_URL, options=options)

    def quit(self) -> None:
        if self.driver is not None:
            self.driver.quit()
            self.driver = None

    def page_source(self) -> str | None:
        if self.driver is None:
            return None
        return str(self.driver.page_source)

    def send_text_and_submit(self, text: str) -> bool:
        if self.driver is None:
            return False

        try:
            from appium.webdriver.common.appiumby import AppiumBy
        except ImportError as exc:
            raise AutomationError("Appium-Python-Client import failed.") from exc

        if self.config.INPUT_ELEMENT_ID:
            input_el = self.driver.find_element(AppiumBy.ID, self.config.INPUT_ELEMENT_ID)
            input_el.click()
            input_el.clear()
            input_el.send_keys(text)
        else:
            human_gaussian_click(
                self.adb_device, self.config.INPUT_BOX_X, self.config.INPUT_BOX_Y
            )
            self.driver.switch_to.active_element.send_keys(text)

        if self.config.SEND_ELEMENT_ID:
            self.driver.find_element(AppiumBy.ID, self.config.SEND_ELEMENT_ID).click()
        else:
            human_gaussian_click(
                self.adb_device, self.config.SEND_BUTTON_X, self.config.SEND_BUTTON_Y
            )
        return True


def _ensure_adb_keyboard_enabled(device: Any, config: Config) -> bool:
    ime = str(config.ADB_KEYBOARD_IME or "").strip()
    if not ime:
        raise ADBCommandError("ADB_KEYBOARD_IME is empty.")

    ime_list = device.shell("ime", "list", "-s", timeout=10, check=False)
    ime_details = device.shell("ime", "list", "-a", timeout=10, check=False)
    if _adb_output_requires_relogin(f"{ime_list}\n{ime_details}") and hasattr(
        device, "_recover_adb_connection"
    ):
        LOGGER.warning("ADB shell requires glogin while checking IMEs; reconnecting.")
        device._recover_adb_connection()
        ime_list = device.shell("ime", "list", "-s", timeout=10, check=False)
        ime_details = device.shell("ime", "list", "-a", timeout=10, check=False)
    if ime not in ime_list and ime not in ime_details:
        apk_path = Path(config.ADB_KEYBOARD_APK_PATH)
        if config.AUTO_INSTALL_ADB_KEYBOARD and apk_path.exists() and hasattr(device, "adb"):
            LOGGER.info("Installing ADBKeyboard from %s.", apk_path)
            install_out = device.adb(["install", "-r", str(apk_path)], timeout=90, check=False)
            LOGGER.info("ADBKeyboard install output: %s", str(install_out).strip()[:240])
            time.sleep(random.uniform(0.6, 1.0))
            ime_list = device.shell("ime", "list", "-s", timeout=10, check=False)
            ime_details = device.shell("ime", "list", "-a", timeout=10, check=False)
            if _adb_output_requires_relogin(f"{ime_list}\n{ime_details}") and hasattr(
                device, "_recover_adb_connection"
            ):
                LOGGER.warning("ADB shell requires glogin after IME install; reconnecting.")
                device._recover_adb_connection()
                ime_list = device.shell("ime", "list", "-s", timeout=10, check=False)
                ime_details = device.shell("ime", "list", "-a", timeout=10, check=False)
        if ime not in ime_list and ime not in ime_details:
            raise ADBCommandError(
                f"ADBKeyboard IME is not installed or visible: {ime}. "
                f"Installed IMEs: {(ime_list or ime_details).strip()}"
            )

    device.shell("ime", "enable", ime, timeout=10, check=False)
    time.sleep(random.uniform(0.15, 0.35))
    device.shell("ime", "set", ime, timeout=10, check=False)
    time.sleep(random.uniform(0.2, 0.45))
    current = device.shell("settings", "get", "secure", "default_input_method", timeout=10, check=False)
    if ime not in current:
        raise ADBCommandError(f"Could not select ADBKeyboard IME; current={current.strip()!r}")
    object.__setattr__(config, "USE_ADB_KEYBOARD", True)
    return True


class DailyActionLimiter:
    def __init__(self, config: Config, *, scope_id: str = "global") -> None:
        self.config = config
        self.scope_id = scope_id

    def assert_available(self) -> None:
        count = self.current_count()
        if count >= self.config.DAILY_ACTION_LIMIT:
            raise DailyLimitExceeded(
                f"Daily limit reached for {self.scope_id}: "
                f"{count}/{self.config.DAILY_ACTION_LIMIT}"
            )

    def consume(self, amount: int = 1) -> int:
        state = self._read_state()
        current = self._count_from_state(state)
        current += amount
        self._write_count_to_state(state, current)
        self.config.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.config.STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return current

    def current_count(self) -> int:
        return self._count_from_state(self._read_state())

    def remaining(self) -> int:
        return max(0, self.config.DAILY_ACTION_LIMIT - self.current_count())

    def _today(self) -> str:
        if ZoneInfo is not None:
            now = dt.datetime.now(ZoneInfo("Asia/Shanghai"))
        else:
            now = dt.datetime.now()
        return now.date().isoformat()

    def _read_state(self) -> dict[str, Any]:
        today = self._today()
        if not self.config.STATE_FILE.exists():
            return {"date": today, "count": 0, "profiles": {}}

        try:
            state = json.loads(self.config.STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            LOGGER.warning("State file is unreadable; resetting daily counter.")
            return {"date": today, "count": 0, "profiles": {}}

        if state.get("date") != today:
            return {"date": today, "count": 0, "profiles": {}}
        state.setdefault("date", today)
        state.setdefault("count", 0)
        state.setdefault("profiles", {})
        return state

    def _count_from_state(self, state: dict[str, Any]) -> int:
        if self.scope_id == "global":
            return int(state.get("count", 0))
        profiles = state.get("profiles")
        if not isinstance(profiles, dict):
            return 0
        profile_state = profiles.get(self.scope_id)
        if isinstance(profile_state, dict):
            return int(profile_state.get("count", 0))
        if isinstance(profile_state, int):
            return int(profile_state)
        return 0

    def _write_count_to_state(self, state: dict[str, Any], count: int) -> None:
        if self.scope_id == "global":
            state["count"] = int(count)
            return
        profiles = state.setdefault("profiles", {})
        if not isinstance(profiles, dict):
            profiles = {}
            state["profiles"] = profiles
        profiles[self.scope_id] = {"count": int(count)}


class ChatHistoryStore:
    def __init__(self, config: Config) -> None:
        self.config = config

    def load(self) -> list[dict[str, str]]:
        if not self.config.HISTORY_FILE.exists():
            return []
        try:
            data = json.loads(self.config.HISTORY_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            LOGGER.warning("History file is unreadable; starting with empty history.")
            return []
        if not isinstance(data, list):
            return []
        return [
            {"role": str(item.get("role", "")), "content": str(item.get("content", ""))}
            for item in data
            if isinstance(item, dict)
        ][-self.config.HISTORY_MAX_TURNS :]

    def append(self, role: str, content: str) -> list[dict[str, str]]:
        history = self.load()
        history.append({"role": role, "content": content})
        history = history[-self.config.HISTORY_MAX_TURNS :]
        self.config.HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.config.HISTORY_FILE.write_text(
            json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return history


def _escape_adb_input_text(text: str) -> str:
    # Android's `input text` treats spaces specially and has shell-sensitive
    # punctuation. Keep this fallback for ASCII only; use Appium for Unicode.
    replacements = {
        " ": "%s",
        "&": r"\&",
        "<": r"\<",
        ">": r"\>",
        "'": r"\'",
        '"': r"\"",
        "\\": r"\\",
        "(": r"\(",
        ")": r"\)",
        ";": r"\;",
        "|": r"\|",
        "*": r"\*",
        "#": r"\#",
    }
    return "".join(replacements.get(ch, ch) for ch in text)


def _normalize_text_for_device_input(text: str, *, allow_unicode: bool) -> str:
    replacements = {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u2026": "...",
        "\u00a0": " ",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    if allow_unicode:
        return re.sub(r"\s+", " ", text).strip()
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", text).strip()


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def human_gaussian_click(
    device: GeelarkADBDevice,
    x: int,
    y: int,
    *,
    sigma_px: float = 5.5,
    max_offset_px: int = 18,
) -> tuple[int, int]:
    """Tap near ``(x, y)`` with a Gaussian-distributed finger offset."""

    width, height = device.get_screen_size()
    dx = _clamp(round(random.gauss(0, sigma_px)), -max_offset_px, max_offset_px)
    dy = _clamp(round(random.gauss(0, sigma_px)), -max_offset_px, max_offset_px)
    target_x = _clamp(int(x + dx), 0, width - 1)
    target_y = _clamp(int(y + dy), 0, height - 1)

    time.sleep(random.uniform(0.06, 0.24))
    device.tap(target_x, target_y)
    time.sleep(random.uniform(0.08, 0.35))
    return target_x, target_y


def human_bezier_swipe(
    device: Any,
    start_coords: tuple[int, int],
    end_coords: tuple[int, int],
    *,
    min_intermediate_points: int = 15,
    total_duration_ms: int | None = None,
    use_motion_events: bool = True,
    segmented_fallback: bool = True,
) -> list[tuple[int, int]]:
    """Swipe along a cubic Bezier path with acceleration and light curvature.

    At least ``min_intermediate_points`` points are generated between the start
    and end coordinates. The function first tries continuous ADB motion events.
    Its fallback can be either short segment swipes along the same curve, or a
    single native drag when callers need to avoid tap-like segmented gestures.
    """

    points = _generate_bezier_points(
        start_coords,
        end_coords,
        min_intermediate_points=min_intermediate_points,
    )
    if total_duration_ms is None:
        distance = math.dist(start_coords, end_coords)
        total_duration_ms = int(_clamp(round(distance * random.uniform(0.55, 0.95)), 420, 1350))

    per_point_delays = _accelerated_delays(len(points), total_duration_ms)

    motion_events_done = False
    if use_motion_events:
        try:
            _run_batched_motion_events(
                device,
                points,
                per_point_delays,
                timeout=max(8.0, total_duration_ms / 1000.0 + 6.0),
            )
            motion_events_done = True
        except ADBCommandError:
            LOGGER.debug(
                "batched motionevent failed; falling back to per-point motionevent.",
                exc_info=True,
            )

    if use_motion_events and not motion_events_done:
        try:
            first = points[0]
            device.motion_event("DOWN", first[0], first[1])
            for point, delay_s in zip(points[1:-1], per_point_delays[1:-1]):
                time.sleep(delay_s)
                device.motion_event("MOVE", point[0], point[1])
            last = points[-1]
            time.sleep(per_point_delays[-1])
            device.motion_event("UP", last[0], last[1])
            motion_events_done = True
        except ADBCommandError:
            LOGGER.debug(
                "motionevent failed; falling back to segmented input swipe.",
                exc_info=True,
            )

    if not motion_events_done and use_motion_events and segmented_fallback:
        # Motion events were attempted but failed; segmented mini-swipes are the
        # best we can do to retry the curve.
        for idx, (start, end) in enumerate(zip(points, points[1:])):
            duration_ms = max(8, int(per_point_delays[min(idx, len(per_point_delays) - 1)] * 1000))
            device.swipe_segment(start, end, duration_ms)
    elif not motion_events_done:
        # Bumble treats a chain of tiny swipe segments like repeated taps on the
        # card/photo surface (each ~25-50ms segment is below Android's tap-vs-drag
        # threshold). When motion events are not in use at all (e.g. api-shell),
        # always use one continuous native drag so the gesture is not interpreted
        # as a tap that could open a chat row or profile picture.
        device.swipe_segment(start_coords, end_coords, total_duration_ms)

    time.sleep(random.uniform(0.12, 0.45))
    return points


def _prefer_motion_events(device: Any) -> bool:
    return not isinstance(device, GeelarkOpenAPIShellDevice)


def _run_batched_motion_events(
    device: Any,
    points: list[tuple[int, int]],
    per_point_delays: list[float],
    *,
    timeout: float,
) -> None:
    if len(points) < 2:
        return

    commands = [f"input motionevent DOWN {points[0][0]} {points[0][1]}"]
    for point, delay_s in zip(points[1:-1], per_point_delays[1:-1]):
        commands.append(f"sleep {max(0.01, delay_s):.3f}")
        commands.append(f"input motionevent MOVE {point[0]} {point[1]}")
    commands.append(f"sleep {max(0.01, per_point_delays[-1]):.3f}")
    commands.append(f"input motionevent UP {points[-1][0]} {points[-1][1]}")
    device.shell("sh", "-c", "; ".join(commands), timeout=timeout)


def _generate_bezier_points(
    start: tuple[int, int],
    end: tuple[int, int],
    *,
    min_intermediate_points: int,
) -> list[tuple[int, int]]:
    x0, y0 = start
    x3, y3 = end
    dx = x3 - x0
    dy = y3 - y0
    distance = max(1.0, math.hypot(dx, dy))

    # Normal vector creates a slight arc. Control points are asymmetric so the
    # path does not collapse into a straight line even for vertical swipes.
    nx = -dy / distance
    ny = dx / distance
    bend = random.choice([-1, 1]) * min(max(distance * random.uniform(0.08, 0.18), 22), 190)

    c1 = (
        x0 + dx * random.uniform(0.22, 0.38) + nx * bend * random.uniform(0.55, 1.05),
        y0 + dy * random.uniform(0.22, 0.38) + ny * bend * random.uniform(0.55, 1.05),
    )
    c2 = (
        x0 + dx * random.uniform(0.62, 0.82) - nx * bend * random.uniform(0.35, 0.85),
        y0 + dy * random.uniform(0.62, 0.82) - ny * bend * random.uniform(0.35, 0.85),
    )

    total_points = max(min_intermediate_points + 2, min(34, int(distance / 140) + 12))
    points: list[tuple[int, int]] = []
    for i in range(total_points):
        raw_t = i / (total_points - 1)
        t = 0.5 - math.cos(math.pi * raw_t) / 2.0
        x, y = _cubic_bezier((x0, y0), c1, c2, (x3, y3), t)
        if 0 < i < total_points - 1:
            x += random.gauss(0, 1.7)
            y += random.gauss(0, 1.7)
        points.append((round(x), round(y)))
    return points


def _cubic_bezier(
    p0: tuple[float, float],
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
    t: float,
) -> tuple[float, float]:
    inv = 1.0 - t
    x = inv**3 * p0[0] + 3 * inv**2 * t * p1[0] + 3 * inv * t**2 * p2[0] + t**3 * p3[0]
    y = inv**3 * p0[1] + 3 * inv**2 * t * p1[1] + 3 * inv * t**2 * p2[1] + t**3 * p3[1]
    return x, y


def _accelerated_delays(point_count: int, total_duration_ms: int) -> list[float]:
    segments = max(1, point_count - 1)
    weights: list[float] = []
    for i in range(segments):
        phase = i / max(1, segments - 1)
        # Higher speed in the middle, slower press and release.
        velocity = 0.42 + math.sin(math.pi * phase) * 1.15
        weights.append(1.0 / velocity)

    total_s = total_duration_ms / 1000.0
    scale = total_s / sum(weights)
    delays = [w * scale * random.uniform(0.86, 1.16) for w in weights]
    return [delays[0], *delays]


def extract_page_context(
    device: GeelarkADBDevice,
    appium: AppiumController | None = None,
) -> PageContext:
    """Scan current UI hierarchy and extract all TextView text content."""

    activity = device.get_current_activity()
    xml_text = None
    if appium is not None:
        try:
            xml_text = appium.page_source()
        except Exception:
            LOGGER.debug("Appium page source failed; falling back to ADB.", exc_info=True)

    if not xml_text:
        xml_text = device.dump_ui_xml()

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise UIExtractionError("Could not parse current UI XML.") from exc

    text_nodes: list[TextNode] = []
    seen: set[tuple[str, str, str]] = set()
    for element in root.iter():
        attrib = element.attrib
        class_name = attrib.get("class", "")
        text = (attrib.get("text") or "").strip()
        if not text or "TextView" not in class_name:
            continue

        node = TextNode(
            text=text,
            class_name=class_name,
            resource_id=attrib.get("resource-id", ""),
            content_desc=attrib.get("content-desc", ""),
            bounds=attrib.get("bounds", ""),
        )
        dedupe_key = (node.text, node.resource_id, node.bounds)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        text_nodes.append(node)

    inferred = _infer_profile_and_message_fields([node.text for node in text_nodes])
    return PageContext(
        activity=activity,
        text_views=text_nodes,
        inferred=inferred,
        captured_at=dt.datetime.now(dt.timezone.utc).isoformat(),
    )


def _infer_profile_and_message_fields(texts: Iterable[str]) -> dict[str, str]:
    cleaned = [text.strip() for text in texts if text and text.strip()]
    inferred: dict[str, str] = {}

    for text in cleaned:
        name_age_match = re.match(r"^([^,\n]{1,40}),\s*(1[8-9]|[2-5]\d|60)$", text)
        if name_age_match:
            inferred["name"] = name_age_match.group(1).strip()
            inferred["age"] = name_age_match.group(2)
            break

    for text in cleaned:
        age_match = re.search(r"(?<!\d)(1[8-9]|[2-5]\d|60)(?:\s*岁|y/o|yo|岁)?(?!\d)", text, re.I)
        if age_match and "age" not in inferred:
            inferred["age"] = age_match.group(1)
            break

    ignored_names = {
        "profile",
        "see profile",
        "complete profile",
        "pay plan",
        "photo insights",
        "safety and wellbeing",
        "discover",
        "people",
        "liked you",
        "chats",
        "back",
        "add",
    }
    for text in cleaned:
        if (
            1 <= len(text) <= 18
            and text.lower() not in ignored_names
            and not re.search(r"\d|[:：]|已读|发送|关注|消息", text)
        ):
            inferred.setdefault("possible_name", text)
            break

    job_keywords = (
        "工程师",
        "设计",
        "医生",
        "老师",
        "律师",
        "运营",
        "产品",
        "经理",
        "学生",
        "teacher",
        "engineer",
        "designer",
        "doctor",
        "manager",
        "student",
    )
    for text in cleaned:
        if any(keyword.lower() in text.lower() for keyword in job_keywords):
            inferred.setdefault("possible_occupation", text)
            break

    message_candidates = [
        text
        for text in cleaned
        if len(text) >= 2 and not re.fullmatch(r"\d{1,2}:\d{2}", text)
    ]
    if message_candidates:
        inferred["last_visible_text"] = message_candidates[-1]

    for text in cleaned:
        height_match = re.search(
            r"((?:1[4-9]\d|20\d)\s*cm|[4-7]\s*(?:'|ft)\s*\d{0,2})",
            text,
            re.I,
        )
        if height_match:
            inferred["height"] = height_match.group(1).strip()
            break

    for text in cleaned:
        weight_match = re.search(r"((?:[4-9]\d|1\d{2})\s*(?:kg|公斤|lbs?|磅))", text, re.I)
        if weight_match:
            inferred["weight"] = weight_match.group(1).strip()
            break

    for idx, text in enumerate(cleaned):
        if text.lower() != "interests":
            continue
        for candidate in cleaned[idx + 1 : idx + 24]:
            if "," in candidate and not any(
                stop in candidate.lower()
                for stop in ("profile", "premium", "photo", "complete")
            ):
                inferred["interests"] = candidate
                break
        if "interests" in inferred:
            break

    return inferred


def screenshot_to_base64(
    device: GeelarkADBDevice,
    *,
    crop_bounds: tuple[int, int, int, int] | None = None,
    save_path: Path | None = None,
) -> str:
    """Capture the current screen or a crop and return PNG Base64 text."""

    png = device.screenshot_png()
    if crop_bounds is not None:
        try:
            from PIL import Image
        except ImportError as exc:
            raise AutomationError("Pillow is required for screenshot cropping.") from exc

        left, top, right, bottom = crop_bounds
        with Image.open(io.BytesIO(png)) as image:
            cropped = image.crop((left, top, right, bottom))
            output = io.BytesIO()
            cropped.save(output, format="PNG")
            png = output.getvalue()

    if save_path is not None:
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_bytes(png)
    return base64.b64encode(png).decode("ascii")


def safe_screenshot_png(device: Any) -> bytes:
    """Capture a screenshot, falling back to file-based ADB pull when needed."""

    try:
        return device.screenshot_png()
    except ADBCommandError:
        if not isinstance(device, GeelarkADBDevice):
            raise
        LOGGER.debug("exec-out screenshot failed; using screencap file fallback.", exc_info=True)

    remote_path = f"/sdcard/geelark_screen_{uuid.uuid4().hex}.png"
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        local_path = Path(tmp.name)
    try:
        device.shell("screencap", "-p", remote_path, timeout=20)
        device.adb(["pull", remote_path, str(local_path)], timeout=45)
        data = local_path.read_bytes()
        if not data.startswith(b"\x89PNG"):
            data = data.replace(b"\r\n", b"\n")
        if not data.startswith(b"\x89PNG"):
            raise ADBCommandError("File-based screencap did not return PNG data.")
        return data
    finally:
        try:
            device.shell("rm", "-f", remote_path, timeout=5, check=False)
        except Exception:
            LOGGER.debug("Failed to remove temporary remote screenshot.", exc_info=True)
        try:
            local_path.unlink(missing_ok=True)
        except Exception:
            LOGGER.debug("Failed to remove temporary local screenshot.", exc_info=True)


def parse_crop_bounds(raw: str) -> tuple[int, int, int, int] | None:
    if not raw:
        return None
    parts = [part.strip() for part in raw.split(",")]
    if len(parts) != 4:
        raise AutomationError(
            "CUSTOMER_PHOTO_CROP_BOUNDS must be four comma-separated integers."
        )
    return tuple(int(part) for part in parts)  # type: ignore[return-value]


def save_png_crop(
    png: bytes,
    save_path: Path,
    crop_bounds: tuple[int, int, int, int] | None,
) -> None:
    save_path.parent.mkdir(parents=True, exist_ok=True)
    if crop_bounds is None:
        save_path.write_bytes(png)
        return

    try:
        from PIL import Image
    except ImportError as exc:
        raise AutomationError("Pillow is required for customer photo cropping.") from exc

    with Image.open(io.BytesIO(png)) as image:
        width, height = image.size
        left, top, right, bottom = crop_bounds
        box = (
            _clamp(left, 0, width),
            _clamp(top, 0, height),
            _clamp(right, 0, width),
            _clamp(bottom, 0, height),
        )
        image.crop(box).save(save_path, format="PNG")


def _timestamp_for_path() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def _randomized_right_swipe_coords(
    config: Config,
    device: Any,
    screen_size: tuple[int, int] | None = None,
) -> tuple[tuple[int, int], tuple[int, int]]:
    width, height = screen_size or device.get_screen_size()
    start = (
        _clamp(
            config.RIGHT_SWIPE_START_X
            + round(random.gauss(0, config.RIGHT_SWIPE_JITTER_X / 2)),
            1,
            width - 2,
        ),
        _clamp(
            config.RIGHT_SWIPE_START_Y
            + round(random.gauss(0, config.RIGHT_SWIPE_JITTER_Y / 2)),
            1,
            height - 2,
        ),
    )
    end = (
        _clamp(
            config.RIGHT_SWIPE_END_X
            + round(random.gauss(0, config.RIGHT_SWIPE_JITTER_X / 2)),
            1,
            width - 2,
        ),
        _clamp(
            config.RIGHT_SWIPE_END_Y
            + round(random.gauss(0, config.RIGHT_SWIPE_JITTER_Y / 2)),
            1,
            height - 2,
        ),
    )
    if end[0] <= start[0] + 320:
        end = (_clamp(start[0] + 520, 1, width - 2), end[1])
    return start, end


def _randomized_bumble_like_drag_coords(
    screen_size: tuple[int, int],
    *,
    strong: bool = False,
) -> tuple[tuple[int, int], tuple[int, int]]:
    width, height = screen_size
    start_x_ratio = random.uniform(0.17, 0.32) if strong else random.uniform(0.20, 0.36)
    start_y_ratio = random.uniform(0.50, 0.61)
    end_x_ratio = random.uniform(0.90, 0.98) if strong else random.uniform(0.86, 0.96)

    start = (
        _clamp(round(width * start_x_ratio + random.gauss(0, width * 0.015)), 1, width - 2),
        _clamp(round(height * start_y_ratio + random.gauss(0, height * 0.012)), 1, height - 2),
    )
    end_y = start[1] + round(random.gauss(-height * 0.035, height * 0.026))
    end = (
        _clamp(round(width * end_x_ratio + random.gauss(0, width * 0.01)), 1, width - 2),
        _clamp(end_y, round(height * 0.43), round(height * 0.65)),
    )
    min_dx = round(width * (0.66 if strong else 0.58))
    if end[0] <= start[0] + min_dx:
        end = (_clamp(start[0] + min_dx, 1, width - 2), end[1])
    return start, end


def _randomized_profile_view_swipe_coords(
    config: Config,
    device: Any,
    *,
    reverse: bool = False,
    screen_size: tuple[int, int] | None = None,
) -> tuple[tuple[int, int], tuple[int, int]]:
    width, height = screen_size or device.get_screen_size()
    start_base = (config.PROFILE_VIEW_START_X, config.PROFILE_VIEW_START_Y)
    end_base = (config.PROFILE_VIEW_END_X, config.PROFILE_VIEW_END_Y)
    if reverse:
        start_base, end_base = end_base, start_base

    start = (
        _clamp(
            start_base[0] + round(random.gauss(0, config.PROFILE_VIEW_JITTER_X / 2)),
            1,
            width - 2,
        ),
        _clamp(
            start_base[1] + round(random.gauss(0, config.PROFILE_VIEW_JITTER_Y / 2)),
            1,
            height - 2,
        ),
    )
    end = (
        _clamp(
            end_base[0] + round(random.gauss(0, config.PROFILE_VIEW_JITTER_X / 2)),
            1,
            width - 2,
        ),
        _clamp(
            end_base[1] + round(random.gauss(0, config.PROFILE_VIEW_JITTER_Y / 2)),
            1,
            height - 2,
        ),
    )
    if abs(start[1] - end[1]) < 420:
        if reverse:
            end = (end[0], _clamp(start[1] + 780, 1, height - 2))
        else:
            end = (end[0], _clamp(start[1] - 780, 1, height - 2))
    return start, end


def perform_random_profile_browse(
    device: Any,
    config: Config,
    *,
    probability: float | None = None,
    max_swipes: int | None = None,
    screen_size: tuple[int, int] | None = None,
) -> list[dict[str, Any]]:
    probability = config.PROFILE_VIEW_PROBABILITY if probability is None else probability
    max_swipes = config.PROFILE_VIEW_MAX_SWIPES if max_swipes is None else max_swipes
    if max_swipes <= 0 or random.random() > max(0.0, min(1.0, probability)):
        return []

    swipe_count = random.randint(1, max_swipes)
    operations: list[dict[str, Any]] = []
    for view_idx in range(swipe_count):
        reverse = view_idx > 0 and random.random() < 0.35
        start, end = _randomized_profile_view_swipe_coords(
            config,
            device,
            reverse=reverse,
            screen_size=screen_size,
        )
        duration_ms = random.randint(620, 1180)
        points = human_bezier_swipe(
            device,
            start,
            end,
            min_intermediate_points=15,
            total_duration_ms=duration_ms,
            use_motion_events=_prefer_motion_events(device),
        )
        operations.append(
            {
                "type": "view_profile_swipe",
                "direction": "down" if reverse else "up",
                "start": start,
                "end": end,
                "duration_ms": duration_ms,
                "point_count": len(points),
            }
        )
        time.sleep(random.uniform(0.45, 1.05))
    return operations


def _profile_text_lines(page_context: PageContext) -> list[str]:
    ignored = {
        "Bumble",
        "Profile",
        "Discover",
        "People",
        "Liked You",
        "Chats",
        "Get offer",
        "Recent",
    }
    lines: list[str] = []
    seen: set[str] = set()
    for node in page_context.text_views:
        text = node.text.strip()
        if not text or text in ignored or text in seen:
            continue
        seen.add(text)
        lines.append(text)
    return lines


def _write_text_lines(path: Path, lines: Iterable[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def _dedupe_strings(values: Iterable[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = value.strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        deduped.append(clean)
    return deduped


def save_current_customer_snapshot(
    device: Any,
    appium: AppiumController | None,
    *,
    output_dir: Path,
    index: int,
    crop_bounds: tuple[int, int, int, int] | None,
) -> tuple[Path, PageContext]:
    page_context = extract_page_context(device, appium)
    stamp = _timestamp_for_path()
    profile_dir = output_dir / f"profile_{index:04d}_{stamp}"
    profile_dir.mkdir(parents=True, exist_ok=True)

    (profile_dir / "context.json").write_text(
        json.dumps(page_context.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    text_lines = _profile_text_lines(page_context)
    (profile_dir / "texts.txt").write_text("\n".join(text_lines), encoding="utf-8")

    png = safe_screenshot_png(device)
    (profile_dir / "screen.png").write_bytes(png)
    save_png_crop(png, profile_dir / "photo.png", crop_bounds)

    summary = {
        "index": index,
        "captured_at": page_context.captured_at,
        "activity": page_context.activity,
        "inferred": page_context.inferred,
        "texts": text_lines,
        "screen": "screen.png",
        "photo": "photo.png",
    }
    (profile_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return profile_dir, page_context


def get_ai_decision_and_reply(
    config: Config,
    page_context: PageContext,
    chat_history: list[dict[str, str]],
    image_base64: str | None,
) -> str:
    """Call vectorengine.ai and return the generated reply text."""

    system_prompt = (
        "你是一个中文移动端聊天辅助 AI。请结合 UI 文本、历史聊天记录和图片内容，"
        "判断对方语气与兴趣点，生成自然、真诚、尊重边界的男性视角回复。"
        "重点观察图片里的环境线索，例如运动、宠物、美食、旅行、工作场景或情绪状态。"
        "回复必须像真人即时聊天：具体、有感染力、不过度夸张、不油腻、不冒充特定真实人物。"
        "只输出最终要发送的一条消息，不要输出分析过程、JSON、标题或引号。"
    )

    context_payload = {
        "ui_text_context": page_context.to_dict(),
        "chat_history": chat_history[-config.HISTORY_MAX_TURNS :],
        "reply_constraints": {
            "language": "zh-CN",
            "style": "自然、轻松、有画面感",
            "max_sentences": 3,
            "avoid": ["模板化开场", "过度亲密", "冒充真人身份", "施压或诱导"],
        },
    }

    user_content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": json.dumps(context_payload, ensure_ascii=False),
        }
    ]
    if image_base64:
        user_content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{image_base64}"},
            }
        )

    payload = {
        "model": config.AI_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.78,
        "top_p": 0.92,
        "max_tokens": 240,
    }

    url = f"{config.AI_BASE_URL}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.AI_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    last_error: Exception | None = None
    for attempt in range(config.AI_MAX_RETRIES + 1):
        try:
            response = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=config.AI_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            reply = _normalize_reply(str(content))
            if not reply:
                raise AIAPIError("AI response was empty after normalization.")
            return reply
        except requests.Timeout as exc:
            last_error = exc
            LOGGER.warning("AI request timed out on attempt %s.", attempt + 1)
        except requests.RequestException as exc:
            last_error = exc
            LOGGER.warning("AI request failed on attempt %s: %s", attempt + 1, exc)
        except (KeyError, IndexError, ValueError, AIAPIError) as exc:
            last_error = exc
            LOGGER.warning("AI response parse failed on attempt %s: %s", attempt + 1, exc)

        if attempt < config.AI_MAX_RETRIES:
            time.sleep(1.2 * (attempt + 1) + random.uniform(0.2, 0.8))

    raise AIAPIError(f"AI API call failed after retries: {last_error}") from last_error


def _normalize_reply(reply: str) -> str:
    reply = reply.strip()
    reply = re.sub(r"^```(?:text|json)?", "", reply, flags=re.I).strip()
    reply = re.sub(r"```$", "", reply).strip()
    reply = reply.strip('"“”')
    return reply[:500]


class AutomationRunner:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.device = GeelarkADBDevice(config)
        self.appium = AppiumController(config, self.device)
        self.limiter = DailyActionLimiter(config)
        self.history_store = ChatHistoryStore(config)

    def start(self) -> None:
        self.device.ensure_connected()
        self.appium.connect()

    def stop(self) -> None:
        self.appium.quit()

    def run_once(self) -> str:
        self.limiter.assert_available()
        page_context = extract_page_context(self.device, self.appium)
        history = self.history_store.load()
        visible_text = page_context.compact_text()
        self.history_store.append("visible_context", visible_text)

        image_base64 = screenshot_to_base64(self.device)
        reply = get_ai_decision_and_reply(
            self.config,
            page_context=page_context,
            chat_history=history,
            image_base64=image_base64,
        )

        self._send_reply(reply)
        self.history_store.append("assistant", reply)
        count = self.limiter.consume()
        LOGGER.info("Sent reply and consumed daily action %s/%s.", count, self.config.DAILY_ACTION_LIMIT)

        if random.random() < 0.7:
            human_bezier_swipe(
                self.device,
                (self.config.SWIPE_START_X, self.config.SWIPE_START_Y),
                (self.config.SWIPE_END_X, self.config.SWIPE_END_Y),
            )
        return reply

    def run_forever(self) -> None:
        self.start()
        try:
            while True:
                try:
                    reply = self.run_once()
                    LOGGER.info("AI reply: %s", reply)
                except DailyLimitExceeded:
                    raise
                except (ADBCommandError, UIExtractionError, AIAPIError, AutomationError):
                    LOGGER.exception("Iteration failed; backing off before retry.")

                wait_s = random.uniform(
                    self.config.RANDOM_WAIT_MIN_SECONDS,
                    self.config.RANDOM_WAIT_MAX_SECONDS,
                )
                LOGGER.info("Sleeping %.2fs before next operation.", wait_s)
                time.sleep(wait_s)

                if not self.config.LOOP_FOREVER:
                    break
        finally:
            self.stop()

    def _send_reply(self, reply: str) -> None:
        try:
            if self.appium.send_text_and_submit(reply):
                return
        except Exception:
            LOGGER.debug("Appium send failed; falling back to ADB.", exc_info=True)

        human_gaussian_click(self.device, self.config.INPUT_BOX_X, self.config.INPUT_BOX_Y)
        self.device.input_text(reply)
        time.sleep(random.uniform(0.25, 0.9))
        human_gaussian_click(self.device, self.config.SEND_BUTTON_X, self.config.SEND_BUTTON_Y)


def run_profile_test(
    config: Config,
    *,
    profile_id: str,
    prepare_adb: bool = False,
    use_api_shell: bool = False,
    do_swipe: bool = False,
    draft_reply: bool = False,
    send_reply: bool = False,
    save_screenshot: bool = True,
) -> None:
    """Diagnostic runner for one GeeLark cloud phone profile."""

    config = replace(
        config,
        GEELARK_PROFILE_ID=profile_id,
        AUTO_START_PHONE=prepare_adb,
        AUTO_ENABLE_ADB=prepare_adb,
        LOOP_FOREVER=False,
    )
    api = GeelarkOpenAPIClient(config)

    status_payload = api.phone_status([profile_id])
    status = _extract_phone_status(status_payload, profile_id)
    LOGGER.info("Cloud phone %s status=%s (0 means started).", profile_id, status)

    output_dir = Path.cwd() / "diagnostics" / profile_id
    output_dir.mkdir(parents=True, exist_ok=True)

    device: Any
    if use_api_shell:
        device = GeelarkOpenAPIShellDevice(config)
    else:
        device = GeelarkADBDevice(config)
    appium = AppiumController(config, device)
    try:
        try:
            device.ensure_connected()
        except ADBCommandError as exc:
            if use_api_shell or "ADB binary not found" not in str(exc):
                raise
            LOGGER.warning("Local adb is unavailable; falling back to OpenAPI shell mode.")
            device = GeelarkOpenAPIShellDevice(config)
            appium = AppiumController(config, device)
            device.ensure_connected()
        appium.connect()

        page_context = extract_page_context(device, appium)
        context_path = output_dir / "page_context.json"
        context_path.write_text(
            json.dumps(page_context.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        LOGGER.info("Activity: %s", page_context.activity or "unknown")
        LOGGER.info("Extracted %s visible TextView node(s).", len(page_context.text_views))
        LOGGER.info("Inferred fields: %s", json.dumps(page_context.inferred, ensure_ascii=False))
        LOGGER.info("Context saved to %s", context_path)

        image_base64 = None
        if save_screenshot:
            screenshot_path = output_dir / "screen.png"
            image_base64 = screenshot_to_base64(device, save_path=screenshot_path)
            LOGGER.info("Screenshot saved to %s", screenshot_path)

        if do_swipe:
            points = human_bezier_swipe(
                device,
                (config.SWIPE_START_X, config.SWIPE_START_Y),
                (config.SWIPE_END_X, config.SWIPE_END_Y),
            )
            swipe_path = output_dir / "last_swipe_points.json"
            swipe_path.write_text(
                json.dumps(points, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            LOGGER.info("Bezier swipe executed with %s points.", len(points))

        if draft_reply or send_reply:
            history_store = ChatHistoryStore(config)
            history = history_store.load()
            reply = get_ai_decision_and_reply(config, page_context, history, image_base64)
            draft_path = output_dir / "reply_draft.txt"
            draft_path.write_text(reply, encoding="utf-8")
            LOGGER.info("Reply draft saved to %s", draft_path)
            LOGGER.info("Reply draft: %s", reply)

            if send_reply:
                runner = AutomationRunner(config)
                runner.device = device
                runner.appium = appium
                runner._send_reply(reply)
                history_store.append("assistant", reply)
                LOGGER.info("Reply was sent.")
            else:
                LOGGER.info("Dry run only; reply was not sent.")
    finally:
        appium.quit()


def _build_device_for_profile(
    config: Config,
    *,
    use_api_shell: bool,
) -> tuple[Any, AppiumController]:
    device: Any
    if use_api_shell:
        device = GeelarkOpenAPIShellDevice(config)
    else:
        device = GeelarkADBDevice(config)
    appium = AppiumController(config, device)
    return device, appium


def _tap_bumble_bottom_nav_from_ui(device: Any, tab: str) -> bool:
    labels = {
        "profile": "profile",
        "discover": "discover",
        "people": "people",
        "liked": "liked you",
        "chats": "chats",
    }
    wanted = labels.get(tab)
    if not wanted:
        return False
    try:
        screen_size = device.get_screen_size()
        nodes = _extract_visible_ui_nodes_fast(device)
    except Exception:
        return False
    width, height = screen_size
    candidates: list[tuple[int, int, int, int]] = []
    for node in nodes:
        text = str(node.get("text") or node.get("content_desc") or "").strip().lower()
        if text != wanted:
            continue
        center = node.get("center")
        if not center:
            continue
        x, y = int(center[0]), int(center[1])
        if not (0 <= x <= width and y >= height * 0.78):
            continue
        rect = _bounds_rect(str(node.get("bounds") or ""))
        area = 0
        if rect:
            left, top, right, bottom = rect
            area = max(0, right - left) * max(0, bottom - top)
            x = _clamp((left + right) // 2, 1, width - 2)
            y = _clamp((top + bottom) // 2, 1, height - 2)
        candidates.append((area, y, x, y))
    if not candidates:
        return False
    candidates.sort(reverse=True)
    _, _, x, y = candidates[0]
    LOGGER.info("Tapping Bumble %s tab from UI node at (%s, %s).", tab, x, y)
    device.shell("input", "tap", x, y, timeout=8, check=False)
    return True


def open_bumble_tab(device: Any, tab: str) -> None:
    """Open Bumble main activity and tap a bottom navigation tab."""

    config = getattr(device, "config", None)
    open_wait_min = getattr(config, "BUMBLE_OPEN_WAIT_MIN_SECONDS", 2.2)
    open_wait_max = getattr(config, "BUMBLE_OPEN_WAIT_MAX_SECONDS", 4.0)
    tab_wait_min = getattr(config, "BUMBLE_TAB_WAIT_MIN_SECONDS", 2.4)
    tab_wait_max = getattr(config, "BUMBLE_TAB_WAIT_MAX_SECONDS", 4.2)

    # If the previous run left Bumble in an editor/detail activity, back out to
    # the main activity so bottom navigation coordinates are meaningful. Skip
    # this preflight in OpenAPI shell mode because dumpsys can hang on freshly
    # started cloud phones; the explicit am start below is cheaper and enough.
    if isinstance(device, GeelarkOpenAPIShellDevice):
        for _ in range(2):
            device.shell("input", "keyevent", "BACK", timeout=6, check=False)
            time.sleep(random.uniform(0.25, 0.55))
    else:
        for _ in range(4):
            activity = device.get_current_activity()
            if "com.bumble.app/" in activity and "AppMainActivity" in activity:
                break
            if activity:
                device.shell("input", "keyevent", "BACK", timeout=6, check=False)
                time.sleep(random.uniform(0.45, 0.95))
                continue
            break

    try:
        LOGGER.info("Opening Bumble main activity.")
        device.shell(
            "am",
            "start",
            "-n",
            "com.bumble.app/.ui.launcher.BumbleLauncherActivity",
            timeout=10,
            check=False,
        )
        time.sleep(random.uniform(0.75, 1.25))
        device.shell(
            "am",
            "start",
            "-n",
            "com.bumble.app/.ui.main.AppMainActivity",
            timeout=10,
            check=False,
        )
    except Exception:
        LOGGER.debug("Direct Bumble main activity start failed.", exc_info=True)
        device.shell(
            "monkey",
            "-p",
            "com.bumble.app",
            "-c",
            "android.intent.category.LAUNCHER",
            "1",
            timeout=10,
            check=False,
        )

    time.sleep(random.uniform(open_wait_min, open_wait_max))
    if not isinstance(device, GeelarkOpenAPIShellDevice):
        activity = device.get_current_activity()
        if "com.bumble.app/" not in activity:
            LOGGER.warning(
                "Bumble did not reach foreground (activity=%s); force-relaunching.",
                activity,
            )
            _force_relaunch_bumble(device, reason="open_tab_not_foreground")
            time.sleep(random.uniform(open_wait_min, open_wait_max))
        activity = device.get_current_activity()
        if "com.bumble.app/" in activity and "AppMainActivity" not in activity:
            LOGGER.info("Bumble opened %s; backing out to main activity.", activity)
            for _ in range(3):
                device.shell("input", "keyevent", "BACK", timeout=6, check=False)
                time.sleep(random.uniform(0.35, 0.75))
                activity = device.get_current_activity()
                if "com.bumble.app/" in activity and "AppMainActivity" in activity:
                    break
            if "AppMainActivity" not in activity:
                device.shell(
                    "am",
                    "start",
                    "-n",
                    "com.bumble.app/.ui.main.AppMainActivity",
                    timeout=10,
                    check=False,
                )
                time.sleep(random.uniform(open_wait_min, open_wait_max))
        activity = device.get_current_activity()
        if "com.bumble.app/" not in activity or "AppMainActivity" not in activity:
            for retry_idx in range(2):
                LOGGER.warning(
                    "Bumble main activity check failed after launch (activity=%s); relaunch retry %s/2.",
                    activity,
                    retry_idx + 1,
                )
                device.shell(
                    "monkey",
                    "-p",
                    "com.bumble.app",
                    "-c",
                    "android.intent.category.LAUNCHER",
                    "1",
                    timeout=10,
                    check=False,
                )
                time.sleep(random.uniform(open_wait_min + 0.5, open_wait_max + 1.0))
                device.shell(
                    "am",
                    "start",
                    "-n",
                    "com.bumble.app/.ui.main.AppMainActivity",
                    timeout=10,
                    check=False,
                )
                time.sleep(random.uniform(open_wait_min + 0.5, open_wait_max + 1.0))
                activity = device.get_current_activity()
                if "com.bumble.app/" in activity and "AppMainActivity" in activity:
                    break
            if "com.bumble.app/" not in activity or "AppMainActivity" not in activity:
                if _visible_ui_looks_like_bumble_main(device):
                    LOGGER.warning(
                        "Bumble activity was not parseable (%s), but visible UI looks like Bumble main.",
                        activity,
                    )
                else:
                    raise AutomationError(f"Bumble main activity did not reach foreground; activity={activity}")
    tab_points = {
        "profile": (110, 2190),
        "discover": (320, 2190),
        "people": (540, 2190),
        "liked": (760, 2190),
        "chats": (970, 2190),
    }
    if tab not in tab_points:
        raise AutomationError(f"Unsupported Bumble tab: {tab}")
    x, y = tab_points[tab]
    if not _tap_bumble_bottom_nav_from_ui(device, tab):
        LOGGER.info("Tapping Bumble %s tab by fallback coordinates.", tab)
        human_gaussian_click(device, x, y, sigma_px=9.0, max_offset_px=28)
    time.sleep(random.uniform(tab_wait_min, tab_wait_max))


def handle_common_popups(device: Any, *, max_rounds: int = 3) -> list[dict[str, Any]]:
    """Dismiss common permission, upsell, and onboarding dialogs.

    This is deliberately limited to known low-risk buttons. It does not persist
    UI text, and it avoids purchase/account/destructive actions.
    """

    handled: list[dict[str, Any]] = []
    for _ in range(max_rounds):
        try:
            if isinstance(device, GeelarkOpenAPIShellDevice):
                xml_text = _dump_ui_xml_for_parse_fast(device)
            else:
                xml_text = device.dump_ui_xml()
        except (ADBCommandError, UIExtractionError):
            LOGGER.debug("Popup UI dump failed.", exc_info=True)
            break

        action = _find_popup_action(xml_text)
        if action is None:
            break

        text = action["text"]
        x, y = action["center"]
        LOGGER.info("Handling popup/action button: %s at (%s,%s).", text or action["resource_id"], x, y)
        human_gaussian_click(device, x, y, sigma_px=6.5, max_offset_px=18)
        handled.append(action)
        time.sleep(random.uniform(0.8, 1.6))
    return handled


def _find_popup_action(xml_text: str) -> dict[str, Any] | None:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    safe_markers = (
        "allow",
        "while using",
        "only this time",
        "continue",
        "ok",
        "got it",
        "i agree",
        "agree",
        "accept",
        "not now",
        "no thanks",
        "maybe later",
        "skip",
        "close",
        "dismiss",
        "以后再说",
        "暂不",
        "稍后",
        "不用",
        "不用了",
        "跳过",
        "继续",
        "知道了",
        "好的",
        "确定",
        "同意",
        "接受",
        "允许",
        "关闭",
    )
    unsafe_markers = (
        "delete",
        "remove",
        "sign out",
        "log out",
        "logout",
        "subscribe",
        "upgrade",
        "premium",
        "boost",
        "spotlight",
        "pay",
        "purchase",
        "buy",
        "删除",
        "退出",
        "注销",
        "订阅",
        "升级",
        "购买",
        "支付",
    )
    preferred_resource_markers = (
        "permission_allow",
        "permission_deny",
        "button",
        "dialog",
    )

    candidates: list[tuple[int, dict[str, Any]]] = []
    for element in root.iter():
        attrib = element.attrib
        text = (attrib.get("text") or "").strip()
        desc = (attrib.get("content-desc") or "").strip()
        resource_id = (attrib.get("resource-id") or "").strip()
        class_name = (attrib.get("class") or "").strip()
        bounds = attrib.get("bounds") or ""
        center = _bounds_center(bounds)
        if center is None:
            continue

        combined = " ".join(part for part in (text, desc, resource_id) if part).lower()
        if not combined or any(marker in combined for marker in unsafe_markers):
            continue
        if not any(marker in combined for marker in safe_markers):
            continue

        clickable = attrib.get("clickable") == "true"
        button_like = "button" in class_name.lower() or any(
            marker in resource_id.lower() for marker in preferred_resource_markers
        )
        if not clickable and not button_like:
            continue

        priority = 10
        if "permission_allow" in resource_id.lower():
            priority = 0
        elif any(marker in combined for marker in ("not now", "no thanks", "maybe later", "不用", "暂不", "稍后")):
            priority = 1
        elif any(marker in combined for marker in ("continue", "ok", "got it", "allow", "继续", "确定", "允许")):
            priority = 2
        elif any(marker in combined for marker in ("close", "dismiss", "skip", "关闭", "跳过")):
            priority = 3

        candidates.append(
            (
                priority,
                {
                    "text": text or desc,
                    "resource_id": resource_id,
                    "class": class_name,
                    "center": center,
                },
            )
        )

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _bounds_center(bounds: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
    if not match:
        return None
    left, top, right, bottom = (int(match.group(i)) for i in range(1, 5))
    if right <= left or bottom <= top:
        return None
    return ((left + right) // 2, (top + bottom) // 2)


def _looks_like_no_swipe_target(page_context: PageContext) -> bool:
    text = "\n".join(node.text for node in page_context.text_views).lower()
    stop_markers = (
        "connections start here",
        "people are waiting to talk to you",
        "keep connecting",
        "no one new around you",
        "you've seen everyone",
        "you’ve seen everyone",
        "that's everyone",
        "that’s everyone",
        "come back later",
    )
    return any(marker in text for marker in stop_markers)


def _snapshot_text_blob(snapshot: dict[str, Any]) -> str:
    return "\n".join(str(value) for value in snapshot.get("texts", []) if str(value)).lower()


def _visible_bumble_card_signature(snapshot: dict[str, Any]) -> dict[str, Any]:
    values: list[str] = []
    for node in snapshot.get("nodes", []):
        for key in ("text", "content_desc"):
            value = str(node.get(key) or "").strip()
            if value:
                values.append(value)

    ignored_exact = {
        "filters",
        "recommend to a friend",
        "send a compliment",
        "send superswipe",
        "profile",
        "discover",
        "people",
        "liked you",
        "chats",
    }
    photo_names: list[str] = []
    name_ages: list[str] = []
    useful_prefix: list[str] = []
    for value in values:
        normalized = re.sub(r"\s+", " ", value).strip()
        if not normalized:
            continue
        lower = normalized.lower()
        if lower in ignored_exact:
            continue
        photo_match = re.match(r"^(.+?)(?:['’]s|’s) main photo$", normalized, flags=re.I)
        if photo_match:
            photo_names.append(photo_match.group(1).strip())
            useful_prefix.append(normalized)
            continue
        if re.match(r"^.{1,48},\s*\d{2}$", normalized):
            name_ages.append(normalized)
            useful_prefix.append(normalized)
            continue
        if len(useful_prefix) < 8 and not any(
            marker in lower
            for marker in (
                "interest,",
                "photo verified",
                "new here",
                "travel mode",
            )
        ):
            useful_prefix.append(normalized)

    primary_photo = photo_names[0] if photo_names else ""
    primary_name_age = name_ages[0] if name_ages else ""
    signature_parts = [part for part in (primary_photo, primary_name_age) if part]
    if not signature_parts:
        signature_parts = useful_prefix[:3]
    signature = " | ".join(signature_parts)
    return {
        "signature": signature,
        "primary_photo_name": primary_photo,
        "primary_name_age": primary_name_age,
        "photo_names": photo_names[:3],
        "name_ages": name_ages[:3],
        "useful_prefix": useful_prefix[:8],
        "has_card": bool(primary_photo or primary_name_age),
    }


def _bumble_like_surface_state(snapshot: dict[str, Any]) -> dict[str, Any]:
    text = _snapshot_text_blob(snapshot)
    match_markers = (
        "it's a match",
        "it’s a match",
        "what a match",
        "they're into you too",
        "they’re into you too",
        "you matched",
        "new match",
        "send a message",
        "say hi",
    )
    hard_stop_markers = (
        "connections start here",
        "no one new around you",
        "you've seen everyone",
        "you’ve seen everyone",
        "that's everyone",
        "that’s everyone",
        "come back later",
    )
    like_limit_markers = (
        "you're out of likes",
        "you’re out of likes",
        "out of likes",
        "likes will refresh",
        "unlimited likes",
    )
    like_feedback_markers = (
        "we've got a sense of what you like",
        "we’ve got a sense of what you like",
        "we'll keep learning as you swipe",
        "we’ll keep learning as you swipe",
    )
    detail_or_photo_markers = (
        "send superswipe",
        "send a compliment",
        "recommend to a friend",
        "photo verified",
        "travel mode",
    )
    return {
        "is_match": any(marker in text for marker in match_markers),
        "is_no_target": any(marker in text for marker in hard_stop_markers),
        "is_like_limit": any(marker in text for marker in like_limit_markers),
        "is_like_feedback": any(marker in text for marker in like_feedback_markers),
        "is_detail_or_photo": any(marker in text for marker in detail_or_photo_markers),
        "text_excerpt": "\n".join(str(value) for value in snapshot.get("texts", [])[:12]),
    }


def _capture_bumble_like_snapshot(device: Any, *, label: str = "") -> dict[str, Any]:
    nodes = _extract_visible_ui_nodes_fast(device)
    texts = _text_values_from_nodes(nodes)
    activity = ""
    try:
        activity = device.get_current_activity()
    except Exception:
        LOGGER.debug("Could not read current activity while verifying Bumble like.", exc_info=True)
    snapshot = {
        "label": label,
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "activity": activity,
        "texts": texts,
        "nodes": nodes,
    }
    snapshot["card"] = _visible_bumble_card_signature(snapshot)
    snapshot["state"] = _bumble_like_surface_state(snapshot)
    return snapshot


def _snapshot_is_terminal_like_state(snapshot: dict[str, Any]) -> bool:
    state = snapshot.get("state") or {}
    return bool(
        state.get("is_no_target")
        or state.get("is_like_limit")
    )


def _recover_bumble_card_surface(
    device: Any,
    *,
    start_tab: str = "people",
    max_back_presses: int = 2,
) -> dict[str, Any]:
    actions: list[str] = []
    snapshot = _capture_bumble_like_snapshot(device, label="recover_initial")
    if (snapshot.get("card") or {}).get("has_card") or _snapshot_is_terminal_like_state(snapshot):
        return {"snapshot": snapshot, "actions": actions}

    state = snapshot.get("state") or {}
    if _snapshot_looks_like_android_launcher(snapshot):
        _force_relaunch_bumble(device, reason="recover_launcher_surface")
        actions.append("force_relaunch_bumble")
        time.sleep(random.uniform(1.2, 2.0))
        try:
            open_bumble_tab(device, start_tab)
            actions.append(f"open_tab:{start_tab}")
        except Exception:
            LOGGER.debug("Could not reopen Bumble after launcher recovery.", exc_info=True)
        time.sleep(random.uniform(0.8, 1.35))
        snapshot = _capture_bumble_like_snapshot(device, label="recover_launcher_relaunch")
        if (snapshot.get("card") or {}).get("has_card") or _snapshot_is_terminal_like_state(snapshot):
            return {"snapshot": snapshot, "actions": actions}
        state = snapshot.get("state") or {}

    if state.get("is_match"):
        clicked = _click_visible_text_fast(device, ("Close", "Keep swiping", "Continue"))
        if clicked:
            actions.append(f"click:{clicked}")
            time.sleep(random.uniform(0.65, 1.15))
            snapshot = _capture_bumble_like_snapshot(device, label="recover_match_close")
            if (snapshot.get("card") or {}).get("has_card") or _snapshot_is_terminal_like_state(snapshot):
                return {"snapshot": snapshot, "actions": actions}

    if not state.get("is_detail_or_photo") and not state.get("is_match"):
        try:
            open_bumble_tab(device, start_tab)
            actions.append(f"open_tab:{start_tab}")
            time.sleep(random.uniform(0.8, 1.35))
            snapshot = _capture_bumble_like_snapshot(device, label="recover_open_tab_first")
            if (snapshot.get("card") or {}).get("has_card") or _snapshot_is_terminal_like_state(snapshot):
                return {"snapshot": snapshot, "actions": actions}
        except Exception:
            LOGGER.debug("Could not reopen Bumble tab before back recovery.", exc_info=True)

    for idx in range(max_back_presses):
        device.shell("input", "keyevent", "BACK", timeout=6, check=False)
        actions.append(f"back:{idx + 1}")
        time.sleep(random.uniform(0.65, 1.15))
        snapshot = _capture_bumble_like_snapshot(device, label=f"recover_back_{idx + 1}")
        if (snapshot.get("card") or {}).get("has_card") or _snapshot_is_terminal_like_state(snapshot):
            return {"snapshot": snapshot, "actions": actions}

    try:
        open_bumble_tab(device, start_tab)
        actions.append(f"open_tab:{start_tab}")
        time.sleep(random.uniform(1.0, 1.8))
        snapshot = _capture_bumble_like_snapshot(device, label="recover_open_tab")
    except Exception:
        LOGGER.debug("Could not reopen Bumble tab while recovering like surface.", exc_info=True)
    return {"snapshot": snapshot, "actions": actions}


def _verify_bumble_like_transition(
    device: Any,
    before_snapshot: dict[str, Any],
    *,
    attempts: int = 5,
    wait_min_seconds: float = 0.55,
    wait_max_seconds: float = 1.15,
) -> dict[str, Any]:
    before_card = before_snapshot.get("card") or _visible_bumble_card_signature(before_snapshot)
    before_signature = str(before_card.get("signature") or "")
    last_snapshot: dict[str, Any] | None = None
    for attempt in range(1, attempts + 1):
        time.sleep(random.uniform(wait_min_seconds, wait_max_seconds))
        after_snapshot = _capture_bumble_like_snapshot(device, label=f"after_like_{attempt}")
        last_snapshot = after_snapshot
        after_card = after_snapshot.get("card") or {}
        after_state = after_snapshot.get("state") or {}
        after_signature = str(after_card.get("signature") or "")
        if after_state.get("is_match"):
            return {
                "verified": True,
                "status": "matched",
                "reason": "match_screen_detected",
                "attempt": attempt,
                "before_card": before_card,
                "after_card": after_card,
                "after_state": after_state,
            }
        if after_state.get("is_like_limit"):
            return {
                "verified": False,
                "status": "like_limit",
                "reason": "like_limit_detected",
                "attempt": attempt,
                "before_card": before_card,
                "after_card": after_card,
                "after_state": after_state,
            }
        if after_state.get("is_no_target"):
            return {
                "verified": True,
                "status": "no_more_people_after_like",
                "reason": "no_target_surface_after_swipe",
                "attempt": attempt,
                "before_card": before_card,
                "after_card": after_card,
                "after_state": after_state,
                "stop_loop": True,
            }
        if after_state.get("is_like_feedback"):
            return {
                "verified": True,
                "status": "like_feedback",
                "reason": "bumble_swipe_feedback_detected",
                "attempt": attempt,
                "before_card": before_card,
                "after_card": after_card,
                "after_state": after_state,
            }
        if not after_card.get("has_card") and after_state.get("is_detail_or_photo"):
            recovery = _recover_bumble_card_surface(device, max_back_presses=2)
            after_snapshot = recovery.get("snapshot") or after_snapshot
            last_snapshot = after_snapshot
            after_card = after_snapshot.get("card") or {}
            after_state = after_snapshot.get("state") or {}
            after_state = {**after_state, "recovery_actions": recovery.get("actions") or []}
            after_signature = str(after_card.get("signature") or "")
            if before_signature and after_signature == before_signature:
                return {
                    "verified": False,
                    "status": "opened_profile_recovered",
                    "reason": "swipe_opened_profile_or_photo_and_returned_to_same_card",
                    "attempt": attempt,
                    "before_card": before_card,
                    "after_card": after_card,
                    "after_state": after_state,
                }
        if not after_card.get("has_card") and not (
            after_state.get("is_match")
            or after_state.get("is_no_target")
            or after_state.get("is_like_limit")
            or after_state.get("is_like_feedback")
        ):
            recovery = _recover_bumble_card_surface(device, max_back_presses=1)
            after_snapshot = recovery.get("snapshot") or after_snapshot
            last_snapshot = after_snapshot
            after_card = after_snapshot.get("card") or {}
            after_state = after_snapshot.get("state") or {}
            after_state = {**after_state, "recovery_actions": recovery.get("actions") or []}
            after_signature = str(after_card.get("signature") or "")
            if after_state.get("is_like_limit"):
                return {
                    "verified": False,
                    "status": "like_limit",
                    "reason": "like_limit_detected_after_blank_recovery",
                    "attempt": attempt,
                    "before_card": before_card,
                    "after_card": after_card,
                    "after_state": after_state,
                }
            if after_state.get("is_no_target"):
                return {
                    "verified": True,
                    "status": "no_more_people_after_like",
                    "reason": "no_target_surface_after_blank_recovery",
                    "attempt": attempt,
                    "before_card": before_card,
                    "after_card": after_card,
                    "after_state": after_state,
                    "stop_loop": True,
                }
        if (
            before_signature
            and after_signature
            and after_signature != before_signature
            and after_card.get("has_card")
        ):
            return {
                "verified": True,
                "status": "card_changed",
                "reason": "visible_card_signature_changed",
                "attempt": attempt,
                "before_card": before_card,
                "after_card": after_card,
                "after_state": after_state,
            }

    last_card = (last_snapshot or {}).get("card") or {}
    last_state = (last_snapshot or {}).get("state") or {}
    return {
        "verified": False,
        "status": "card_unchanged",
        "reason": "visible_card_signature_did_not_change_after_swipe",
        "attempt": attempts,
        "before_card": before_card,
        "after_card": last_card,
        "after_state": last_state,
    }


def save_bumble_self_profile(
    device: Any,
    appium: AppiumController | None,
    *,
    output_dir: Path,
    phone_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    profile_dir = output_dir / "self_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    open_bumble_tab(device, "profile")
    dismissed = _click_visible_text_fast(
        device,
        ("Close",),
    )
    if dismissed:
        LOGGER.info("Dismissed self profile popup/action by text: %s.", dismissed)
        time.sleep(random.uniform(0.55, 0.95))

    screen_size = device.get_screen_size()
    text_lines: list[str] = []
    capture_steps: list[dict[str, Any]] = []

    def append_texts(values: Iterable[str]) -> None:
        seen = set(text_lines)
        for value in values:
            clean = value.strip()
            if clean and clean not in seen:
                seen.add(clean)
                text_lines.append(clean)

    def collect_step(label: str) -> list[str]:
        LOGGER.info("Collecting self profile text at step: %s.", label)
        try:
            values = _extract_visible_text_values_fast(device)
        except (ADBCommandError, UIExtractionError, AutomationError) as exc:
            capture_steps.append({"label": label, "status": "failed", "error": str(exc)})
            return []
        append_texts(values)
        capture_steps.append(
            {
                "label": label,
                "status": "captured",
                "text_count": len(values),
                "texts": values,
            }
        )
        LOGGER.info("Self profile step %s extracted %s text value(s).", label, len(values))
        return values

    profile_values = collect_step("profile_tab")
    if not _looks_like_bumble_self_profile(profile_values):
        LOGGER.warning("Current page does not look like own Bumble profile; reopening profile tab once.")
        text_lines.clear()
        capture_steps.clear()
        open_bumble_tab(device, "profile")
        dismissed = _click_visible_text_fast(device, ("Close",))
        if dismissed:
            LOGGER.info("Dismissed self profile popup/action by text after retry: %s.", dismissed)
            time.sleep(random.uniform(0.55, 0.95))
        profile_values = collect_step("profile_tab_retry")

    if not _looks_like_bumble_self_profile(profile_values):
        summary = {
            "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "phone": phone_metadata or {},
            "texts": [],
            "steps": [
                {
                    "label": step.get("label"),
                    "status": step.get("status"),
                    "text_count": step.get("text_count", 0),
                    "error": step.get("error", ""),
                }
                for step in capture_steps
            ],
            "status": "capture_skipped_not_self_profile",
            "error": "Visible page did not match own Bumble profile; skipped to avoid saving customer data.",
            "method": "fast_visible_text",
        }
        (profile_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (profile_dir / "texts.txt").write_text("", encoding="utf-8")
        LOGGER.warning("Skipped self profile capture because current page was not own profile.")
        return summary

    clicked_label = _click_visible_text_fast(
        device,
        (
            "Complete profile",
            "Edit profile",
            "Preview profile",
            "View profile",
            "Your profile",
        ),
    )
    if clicked_label:
        LOGGER.info("Opened self profile surface by UI text: %s.", clicked_label)
        time.sleep(random.uniform(0.75, 1.35))
        collect_step(f"opened_{clicked_label}")
    else:
        LOGGER.info("Could not find a profile edit text target; using fallback profile tap.")
        point = _relative_point(screen_size, 0.50, 0.25)
        human_gaussian_click(device, point[0], point[1], sigma_px=11.0, max_offset_px=34)
        time.sleep(random.uniform(0.75, 1.35))
        collect_step("fallback_profile_tap")

    scroll_count = max(0, int(getattr(device.config, "SELF_PROFILE_SCROLLS", 4)))
    stagnant_scrolls = 0
    for scroll_idx in range(scroll_count):
        start = _relative_point(screen_size, 0.50, 0.78)
        end = _relative_point(screen_size, 0.50, 0.31)
        before_count = len(text_lines)
        LOGGER.info(
            "Scrolling self profile surface %s/%s.",
            scroll_idx + 1,
            scroll_count,
        )
        human_bezier_swipe(
            device,
            start,
            end,
            min_intermediate_points=15,
            total_duration_ms=random.randint(640, 980),
            use_motion_events=_prefer_motion_events(device),
        )
        time.sleep(random.uniform(0.55, 1.05))
        label = f"scroll_{scroll_idx + 1}"
        collect_step(label)
        if len(text_lines) == before_count:
            stagnant_scrolls += 1
        else:
            stagnant_scrolls = 0
        if stagnant_scrolls >= 2:
            LOGGER.info("Stopping self profile scrolls after %s no-new-text pass(es).", stagnant_scrolls)
            break

    captured_activity = device.get_current_activity()
    # Leave the account/detail surface before the main liking flow opens People.
    for _ in range(2):
        device.shell("input", "keyevent", "BACK", timeout=6, check=False)
        time.sleep(random.uniform(0.25, 0.55))

    last_error = ""
    for attempt in range(1):
        try:
            if not text_lines:
                raise UIExtractionError("No visible profile text values were extracted.")
            (profile_dir / "texts.txt").write_text("\n".join(text_lines), encoding="utf-8")

            summary = {
                "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "activity": captured_activity,
                "phone": phone_metadata or {},
                "inferred": _infer_profile_and_message_fields(text_lines),
                "texts": text_lines,
                "steps": capture_steps,
                "status": "captured",
                "method": "fast_visible_text",
            }
            (profile_dir / "summary.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (profile_dir / "context.json").write_text(
                json.dumps(
                    {
                        "activity": summary["activity"],
                        "captured_at": summary["captured_at"],
                        "text_values": text_lines,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            LOGGER.info("Saved self profile context to %s.", profile_dir)
            return summary
        except (ADBCommandError, UIExtractionError, AutomationError) as exc:
            last_error = str(exc)
            LOGGER.warning(
                "Self profile capture attempt %s failed: %s",
                attempt + 1,
                last_error,
            )

    summary = {
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "phone": phone_metadata or {},
        "texts": [],
        "steps": capture_steps,
        "status": "capture_failed",
        "error": last_error,
        "method": "fast_visible_text",
    }
    (profile_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (profile_dir / "texts.txt").write_text("", encoding="utf-8")
    LOGGER.warning("Self profile capture failed; saved error summary to %s.", profile_dir)
    return summary


def _looks_like_bumble_self_profile(values: Iterable[str]) -> bool:
    normalized = {value.strip().lower() for value in values if value and value.strip()}
    if not normalized:
        return False
    positive_markers = (
        "complete profile",
        "pay plan",
        "photo insights",
        "safety and wellbeing",
        "profile strength",
        "finish profile",
        "your profile is",
    )
    return any(
        any(marker in value for marker in positive_markers)
        for value in normalized
    )


def _relative_point(screen_size: tuple[int, int], x_ratio: float, y_ratio: float) -> tuple[int, int]:
    width, height = screen_size
    return (
        _clamp(round(width * x_ratio), 1, width - 2),
        _clamp(round(height * y_ratio), 1, height - 2),
    )


def _extract_visible_text_values_fast(device: Any) -> list[str]:
    if isinstance(device, GeelarkOpenAPIShellDevice):
        dump_path = "/sdcard/window_dump.xml"
        for dump_cmd in (
            f"uiautomator dump --compressed {dump_path}",
            f"uiautomator dump {dump_path}",
        ):
            device._execute(dump_cmd, check=False)
            raw = _read_ui_dump_text_fast(device, dump_path)
            values = _parse_text_values_from_ui_dump(raw)
            if values:
                break
        else:
            values = []
    else:
        xml_text = device.dump_ui_xml()
        values = _parse_text_values_from_ui_dump(xml_text)

    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def _extract_visible_ui_nodes_fast(device: Any, *, limit: int = 320) -> list[dict[str, Any]]:
    if isinstance(device, GeelarkOpenAPIShellDevice):
        dump_path = "/sdcard/window_dump.xml"
        grep_patterns = (
            (
                "resource",
                "connections|connection|chat|message|Conversation|conversation|"
                "your move|Your move|reply|Reply|amber|Amber|yoonsu|Yoonsu|"
                "stephy|Stephy",
            ),
            ("text", "text=\"[^\"]+\"|content-desc=\"[^\"]+\""),
        )
        for dump_cmd in (
            f"uiautomator dump --compressed {dump_path}",
            f"uiautomator dump {dump_path}",
        ):
            device._execute(dump_cmd, check=False)
            collected: list[dict[str, Any]] = []
            for _label, pattern in grep_patterns:
                raw = device._execute(
                    "grep -i -E -o "
                    f"{shlex.quote('<node[^>]*(' + pattern + ')[^>]*>')} "
                    f"{dump_path} | head -{int(limit)}",
                    check=False,
                )
                collected.extend(_parse_ui_node_lines(raw))
            if collected:
                return _dedupe_ui_nodes(collected)
            raw = device._execute(
                f"grep -o '<node[^>]*>' {dump_path} | head -{int(limit)}",
                check=False,
            )
            nodes = _parse_ui_node_lines(raw)
            if nodes:
                return _dedupe_ui_nodes(nodes)
        return []

    xml_text = device.dump_ui_xml()
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise UIExtractionError("Could not parse UI XML for visible nodes.") from exc

    nodes: list[dict[str, Any]] = []
    for element in root.iter():
        attrib = dict(element.attrib)
        nodes.append(_normalize_ui_node_attrs(attrib))
    return _dedupe_ui_nodes(nodes)


def _parse_ui_node_lines(raw: str) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for line in raw.splitlines():
        attrs = {
            key: html.unescape(value)
            for key, value in re.findall(r'([A-Za-z0-9_:-]+)="([^"]*)"', line)
        }
        if attrs:
            nodes.append(_normalize_ui_node_attrs(attrs))
    return nodes


def _normalize_ui_node_attrs(attrs: dict[str, str]) -> dict[str, Any]:
    text = (attrs.get("text") or "").strip()
    desc = (attrs.get("content-desc") or "").strip()
    bounds = attrs.get("bounds") or ""
    center = _bounds_center(bounds)
    return {
        "text": text,
        "content_desc": desc,
        "resource_id": (attrs.get("resource-id") or "").strip(),
        "class": (attrs.get("class") or "").strip(),
        "bounds": bounds,
        "center": center,
        "clickable": attrs.get("clickable") == "true",
        "enabled": attrs.get("enabled", "true") == "true",
        "raw": attrs,
    }


def _dedupe_ui_nodes(nodes: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for node in nodes:
        key = (
            str(node.get("text") or ""),
            str(node.get("content_desc") or ""),
            str(node.get("resource_id") or ""),
            str(node.get("bounds") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(node)
    return deduped


def _text_values_from_nodes(nodes: Iterable[dict[str, Any]]) -> list[str]:
    values: list[str] = []
    for node in nodes:
        text = str(node.get("text") or "").strip()
        desc = str(node.get("content_desc") or "").strip()
        if text:
            values.append(text)
        if desc and desc != text:
            values.append(desc)
    return _dedupe_strings(values)


def _dump_ui_xml_for_parse_fast(device: GeelarkOpenAPIShellDevice) -> str:
    dump_path = "/sdcard/window_dump.xml"
    for dump_cmd in (
        f"uiautomator dump --compressed {dump_path}",
        f"uiautomator dump {dump_path}",
    ):
        device._execute(dump_cmd, check=False)
        raw = _clean_ui_xml_text(device._execute(f"cat {dump_path}", check=False))
        if "<hierarchy" in raw:
            return raw
    raise UIExtractionError("Fast UI XML dump did not return a hierarchy.")


def _clean_ui_xml_text(raw: str) -> str:
    raw = raw.strip()
    # The shell endpoint can prepend status lines after the XML declaration.
    # Starting at the hierarchy root is more robust than keeping <?xml ...?>.
    start = raw.find("<hierarchy")
    if start < 0:
        start = raw.find("<?xml")
    if start >= 0:
        raw = raw[start:]

    end = raw.rfind("</hierarchy>")
    if end >= 0:
        raw = raw[: end + len("</hierarchy>")]

    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", raw)


def _click_visible_text_fast(device: Any, labels: Iterable[str]) -> str | None:
    labels_list = [label for label in labels if label]
    if not labels_list:
        return None

    if isinstance(device, GeelarkOpenAPIShellDevice):
        remote_match = _find_text_center_by_remote_grep(device, labels_list)
        if remote_match is not None:
            label, center = remote_match
            human_gaussian_click(device, center[0], center[1], sigma_px=7.5, max_offset_px=22)
            return label
        return None

    try:
        xml_text = device.dump_ui_xml()
    except (ADBCommandError, UIExtractionError) as exc:
        LOGGER.warning("Could not dump UI XML for text click: %s", exc)
        return None

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        LOGGER.warning("Could not parse UI XML for text click: %s", exc)
        return None

    elements = list(root.iter())
    for label in labels_list:
        label_lower = label.lower()
        for element in elements:
            attrib = element.attrib
            value = " ".join(
                part
                for part in (
                    attrib.get("text") or "",
                    attrib.get("content-desc") or "",
                    attrib.get("resource-id") or "",
                )
                if part
            )
            if not value or label_lower not in value.lower():
                continue
            center = _bounds_center(attrib.get("bounds") or "")
            if center is None:
                continue
            human_gaussian_click(device, center[0], center[1], sigma_px=7.5, max_offset_px=22)
            return label
    return None


def _find_text_center_by_remote_grep(
    device: GeelarkOpenAPIShellDevice,
    labels: Sequence[str],
) -> tuple[str, tuple[int, int]] | None:
    dump_path = "/sdcard/window_dump.xml"
    for dump_cmd in (
        f"uiautomator dump --compressed {dump_path}",
        f"uiautomator dump {dump_path}",
    ):
        device._execute(dump_cmd, check=False)
        for label in labels:
            if not label:
                continue
            # Read only the matching node instead of returning the whole XML
            # through the OpenAPI shell endpoint.
            pattern = f"<node[^>]*{label}[^>]*>"
            raw = device._execute(
                f"grep -i -o {shlex.quote(pattern)} {dump_path} | head -1",
                check=False,
            )
            for line in raw.splitlines():
                node = html.unescape(line).strip()
                if label.lower() not in node.lower():
                    continue
                center = _bounds_center_from_node_text(node)
                if center is not None:
                    return label, center
    return None


def _bounds_center_from_node_text(node: str) -> tuple[int, int] | None:
    bounds_match = re.search(r'bounds="(\[[^\"]+\])"', node)
    if not bounds_match:
        return None
    return _bounds_center(bounds_match.group(1))


def _looks_like_bumble_chat_list(values: Iterable[str]) -> bool:
    normalized = {value.strip().lower() for value in values if value and value.strip()}
    if not normalized:
        return False
    direct_markers = {
        "your matches",
        "new matches",
        "recent",
        "your opening moves",
        "your move",
    }
    if normalized.intersection(direct_markers):
        return True
    return any(
        marker in value
        for value in normalized
        for marker in (
            "new match",
            "conversation expires",
            "hours to reply",
            "it's your turn to message",
            "it’s your turn to message",
        )
    )


def _snapshot_looks_like_bumble_chat_list(snapshot: dict[str, Any]) -> bool:
    if _looks_like_bumble_chat_list(snapshot.get("texts", [])):
        return True
    for node in snapshot.get("nodes", []) or []:
        resource_id = str(node.get("resource_id") or "").lower()
        if any(
            marker in resource_id
            for marker in (
                "connectionsitem_personname",
                "connectionsitem_message",
                "connectionitem_badge",
            )
        ):
            return True
    return False


def _text_values_look_like_android_launcher(values: Iterable[str]) -> bool:
    normalized = {value.strip().lower() for value in values if value and value.strip()}
    if not normalized:
        return False
    launcher_markers = {
        "search",
        "gallery",
        "play store",
        "home",
        "phone",
        "messaging",
        "music",
        "chrome",
        "camera",
    }
    return len(normalized.intersection(launcher_markers)) >= 4


def _snapshot_looks_like_android_launcher(snapshot: dict[str, Any]) -> bool:
    activity = str(snapshot.get("activity") or "").lower()
    if "launcher" in activity:
        return True
    return _text_values_look_like_android_launcher(snapshot.get("texts", []))


def _force_relaunch_bumble(device: Any, *, reason: str = "") -> None:
    if reason:
        LOGGER.info("Force-relaunching Bumble (%s).", reason)
    device.shell("am", "force-stop", "com.bumble.app", timeout=8, check=False)
    time.sleep(random.uniform(0.55, 1.0))
    device.shell(
        "monkey",
        "-p",
        "com.bumble.app",
        "-c",
        "android.intent.category.LAUNCHER",
        "1",
        timeout=10,
        check=False,
    )
    time.sleep(random.uniform(1.2, 2.0))
    device.shell(
        "am",
        "start",
        "-n",
        "com.bumble.app/.ui.launcher.BumbleLauncherActivity",
        timeout=10,
        check=False,
    )
    time.sleep(random.uniform(1.0, 1.7))
    device.shell(
        "am",
        "start",
        "-n",
        "com.bumble.app/.ui.main.AppMainActivity",
        timeout=10,
        check=False,
    )


def _visible_ui_looks_like_bumble_main(device: Any) -> bool:
    try:
        values = _extract_visible_text_values_fast(device)
    except Exception:
        return False
    normalized = {value.strip().lower() for value in values if value and value.strip()}
    nav_markers = {"profile", "discover", "people", "liked you", "chats"}
    return len(normalized.intersection(nav_markers)) >= 3 or _looks_like_bumble_chat_list(values)


def _looks_like_empty_bumble_chat_list(values: Iterable[str]) -> bool:
    text = "\n".join(value.strip().lower() for value in values if value and value.strip())
    if _looks_like_bumble_profile_surface(values):
        return False
    markers = (
        "there's no time like now",
        "there’s no time like now",
        "keep connecting",
        "connections start here",
        "start swiping",
        "when you like each other",
        "here’s where you can chat",
        "here's where you can chat",
        "find your person",
    )
    return any(marker in text for marker in markers)


def _looks_like_bumble_profile_surface(values: Iterable[str]) -> bool:
    text = "\n".join(value.strip().lower() for value in values if value and value.strip())
    markers = (
        "about me",
        "my basics",
        "basics",
        "interests",
        "lifestyle",
        "looking for",
        "height",
        "exercise",
        "education",
        "work",
        "job",
        "occupation",
        "lives in",
        "located in",
        "hometown",
        "zodiac",
        "religion",
        "politics",
        "drinking",
        "smoking",
        "kids",
        "relationship",
        "main photo",
        "photo verified",
        "send a compliment",
        "send superswipe",
        "things we can talk about",
        "recommend to a friend",
        "report and block",
        "hide & report",
    )
    return any(marker in text for marker in markers)


def _looks_like_bumble_chat_thread(values: Iterable[str], activity: str = "") -> bool:
    text = "\n".join(value.strip().lower() for value in values if value and value.strip())
    normalized = {value.strip().lower() for value in values if value and value.strip()}
    activity_lower = activity.lower()
    if _looks_like_bumble_chat_list(values):
        return False
    if "conversation" in activity_lower:
        header_markers = {"view profile", "voice call", "video call"}
        if not normalized:
            return True
        return bool(normalized.intersection(header_markers)) or not _looks_like_bumble_profile_surface(values)
    if "profile" in activity_lower and "conversation" not in activity_lower:
        return False
    if _looks_like_bumble_profile_surface(values):
        return False
    markers = (
        "view profile",
        "voice call",
        "video call",
        "type a message",
        "send a message",
        "you matched",
        "voice note",
        "voice message",
        "video chat",
        "message...",
        "send message",
    )
    if any(marker in text for marker in markers):
        return True
    if "chat" in activity_lower and "appmainactivity" not in activity_lower:
        return not _looks_like_bumble_profile_surface(values)
    return False


def _looks_like_bumble_non_chat_surface(values: Iterable[str], activity: str = "") -> bool:
    text = "\n".join(value.strip().lower() for value in values if value and value.strip())
    activity_lower = activity.lower()
    if _looks_like_bumble_chat_list(values):
        return False
    if (
        "screenstoryblockersactivity" in activity_lower
        or "screenstorylauncheractivity" in activity_lower
        or "photobrowseractivity" in activity_lower
        or "basicfiltersactivity" in activity_lower
        or "payment" in activity_lower
        or "paywall" in activity_lower
    ):
        return True
    markers = (
        "match with people who don’t say yes to just anyone",
        "match with people who don't say yes to just anyone",
        "get the vip treatment",
        "see who likes you",
        "premium+",
        "premium",
        "spotlight",
        "superswipe",
        "send superswipe",
        "narrow your search",
        "basic filters",
        "advanced filters",
        "who would you like to date",
        "one-time payment",
        "by purchasing, you agree",
        "this transaction and our terms",
        "restore purchase",
    )
    return any(marker in text for marker in markers)


def _find_chat_list_candidates(
    nodes: Iterable[dict[str, Any]],
    *,
    screen_size: tuple[int, int],
) -> list[dict[str, Any]]:
    width, height = screen_size
    node_list = list(nodes)
    ignored_exact = {
        "profile",
        "discover",
        "people",
        "liked you",
        "chats",
        "filters",
        "your matches",
        "your opening moves",
        "new matches",
        "recent",
        "bumble",
        "search",
        "close",
        "close sheet",
        "continue",
        "not now",
        "no thanks",
        "get started",
        "today",
        "yesterday",
    }
    ignored_contains = (
        "premium",
        "spotlight",
        "superswipe",
        "complete profile",
        "photo insights",
        "safety and wellbeing",
        "visit help hub",
        "settings",
        "remember photo locations",
        "tag your photos",
        "people are waiting",
        "like them back",
        "people like you",
        "view who likes you",
        "see who",
        "opening moves",
        "green and red flags",
        "wouldn’t know",
        "wouldn't know",
        "conversation expired",
        "expired match",
        "there's no time",
        "there’s no time",
        "keep connecting",
        "connections start here",
        "start swiping",
        "when you like each other",
        "here’s where you can chat",
        "here's where you can chat",
        "recommend to a friend",
        "last message:",
        "dream dinner party guest",
        "what have you been cooking lately",
        "find your person",
        "be seen up to",
        "you have 24 hours to reply",
        "hours to reply",
        "conversation expires in",
        "it's your turn to message them back",
        "it’s your turn to message them back",
    )

    expired_row_y: list[int] = []
    for node in node_list:
        center = node.get("center")
        if center is None:
            continue
        value = " ".join(
            str(node.get(key) or "")
            for key in ("text", "content_desc")
            if str(node.get(key) or "").strip()
        ).lower()
        if "expired" in value:
            expired_row_y.append(int(center[1]))

    candidates: list[dict[str, Any]] = []
    for node in node_list:
        center = node.get("center")
        if center is None:
            continue
        x, y = center
        if not (80 <= x <= width - 80 and 220 <= y <= height - 330):
            continue
        label = str(node.get("text") or node.get("content_desc") or "").strip()
        if not label:
            continue
        lower = label.lower()
        resource_id = str(node.get("resource_id") or "").strip().lower()
        row_values: list[str] = []
        row_has_unread_badge = False
        row_ring_center: tuple[int, int] | None = None
        for sibling in node_list:
            sibling_center = sibling.get("center")
            if not sibling_center or abs(int(sibling_center[1]) - int(y)) > 115:
                continue
            sibling_text = " ".join(
                str(sibling.get(key) or "")
                for key in ("text", "content_desc")
                if str(sibling.get(key) or "").strip()
            )
            if sibling_text:
                row_values.append(sibling_text.lower())
            sibling_resource = str(sibling.get("resource_id") or "").lower()
            if "badgeunread" in sibling_resource:
                row_has_unread_badge = True
            if "connectionitem_ringview" in sibling_resource:
                row_ring_center = (int(sibling_center[0]), int(sibling_center[1]))
        row_text = "\n".join(row_values)
        is_connection_item = any(
            marker in resource_id
            for marker in (
                "connectionsitem_personname",
                "connectionsitem_message",
                "connectionitem_ringview",
            )
        )
        if "expired" in lower:
            continue
        if any(abs(int(y) - expired_y) <= 190 for expired_y in expired_row_y):
            continue
        if re.fullmatch(r"[a-z]{3,9} \d{1,2}, \d{4}", lower):
            continue
        if lower in ignored_exact or any(marker in lower for marker in ignored_contains):
            continue
        if (
            re.fullmatch(r"\d{1,2}:\d{2}", lower)
            or re.fullmatch(r"\d+", lower)
            or re.fullmatch(r"\(\d+\)", lower)
        ):
            continue
        if len(label) > 90:
            continue
        if "connectionitem_ringview" in resource_id:
            row_center = (_clamp(x, 1, width - 2), _clamp(y, 1, height - 2))
        elif (
            "connectionsitem_personname" in resource_id
            or "connectionsitem_message" in resource_id
        ):
            row_center = (_clamp(x, 1, width - 2), _clamp(y, 1, height - 2))
        elif row_ring_center is not None:
            row_center = (
                _clamp(row_ring_center[0], 1, width - 2),
                _clamp(row_ring_center[1], 1, height - 2),
            )
        else:
            row_center = (_clamp(width // 2, 1, width - 2), _clamp(y, 1, height - 2))
        is_top_match_carousel = "connectionitem_ringview" in resource_id and y < height * 0.30
        if "your move" in row_text:
            priority = -3
        elif row_has_unread_badge:
            priority = -2
        elif "connectionsitem_personname" in resource_id or "connectionsitem_message" in resource_id:
            priority = 0
        elif is_connection_item:
            priority = 4 if is_top_match_carousel else 1
        else:
            priority = 2
        candidates.append(
            {
                "label": label,
                "node_center": center,
                "click_center": row_center,
                "bounds": node.get("bounds"),
                "resource_id": node.get("resource_id"),
                "priority": priority,
                "row_context": row_values[:6],
            }
        )

    candidates.sort(key=lambda item: (item["priority"], item["click_center"][1], len(item["label"])))
    deduped: list[dict[str, Any]] = []
    seen_row_y: list[int] = []
    for candidate in candidates:
        candidate_y = int(candidate["click_center"][1])
        if any(abs(candidate_y - existing_y) <= 125 for existing_y in seen_row_y):
            continue
        seen_row_y.append(candidate_y)
        deduped.append(candidate)
    return deduped


def _blind_chat_list_fallback_candidates(
    *,
    screen_size: tuple[int, int],
    max_rows: int = 8,
) -> list[dict[str, Any]]:
    width, height = screen_size
    candidates: list[dict[str, Any]] = []
    carousel_y = _clamp(round(height * 0.195), 300, height - 360)
    carousel_x_ratios = (0.18, 0.36, 0.54, 0.72, 0.88)
    for idx, ratio in enumerate(carousel_x_ratios, start=1):
        point = (_clamp(round(width * ratio), 1, width - 2), carousel_y)
        candidates.append(
            {
                "label": f"blind_match_card_{idx}",
                "node_center": point,
                "click_center": point,
                "bounds": "",
                "resource_id": "blind_chat_list_fallback",
                "priority": 7,
                "source": "blind_fallback",
            }
        )
    row_ratios = (0.28, 0.36, 0.44, 0.52, 0.60, 0.68, 0.76)
    for idx, ratio in enumerate(row_ratios, start=1):
        y = _clamp(round(height * ratio), 240, height - 360)
        point = (_clamp(round(width * 0.50), 1, width - 2), y)
        candidates.append(
            {
                "label": f"blind_row_{idx}",
                "node_center": point,
                "click_center": point,
                "bounds": "",
                "resource_id": "blind_chat_list_fallback",
                "priority": 8,
                "source": "blind_fallback",
            }
        )
    return candidates[:max_rows]


def _collect_visible_text_snapshot(
    device: Any,
    *,
    label: str,
    output_dir: Path,
) -> dict[str, Any]:
    nodes = _extract_visible_ui_nodes_fast(device)
    texts = _text_values_from_nodes(nodes)
    activity = device.get_current_activity()
    snapshot = {
        "label": label,
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "activity": activity,
        "texts": texts,
        "nodes": [
            {
                "text": node.get("text"),
                "content_desc": node.get("content_desc"),
                "resource_id": node.get("resource_id"),
                "class": node.get("class"),
                "bounds": node.get("bounds"),
                "center": node.get("center"),
                "clickable": node.get("clickable"),
                "enabled": node.get("enabled"),
            }
            for node in nodes
        ],
    }
    (output_dir / f"{label}.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _write_text_lines(output_dir / f"{label}.txt", texts)
    return snapshot


def _save_screen_if_requested(device: Any, save_path: Path, enabled: bool) -> str:
    if not enabled:
        return ""
    if isinstance(device, GeelarkOpenAPIShellDevice):
        LOGGER.warning(
            "Skipping screenshot %s in OpenAPI shell mode; PNG transfer is too slow.",
            save_path,
        )
        return ""
    try:
        png = safe_screenshot_png(device)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_bytes(png)
        return str(save_path)
    except Exception as exc:
        LOGGER.warning("Failed to save screenshot %s: %s", save_path, exc)
        return ""


def _matched_profile_click_points(
    snapshot: dict[str, Any],
    *,
    screen_size: tuple[int, int],
) -> list[tuple[int, int]]:
    width, height = screen_size
    points: list[tuple[int, int]] = []
    for node in snapshot.get("nodes", []):
        center = node.get("center")
        if not center:
            continue
        x, y = int(center[0]), int(center[1])
        if not (0 <= x < width and 0 <= y <= height * 0.14):
            continue
        content_desc = str(node.get("content_desc") or "").strip().lower()
        resource_id = str(node.get("resource_id") or "").strip().lower()
        if content_desc == "view profile" or "chattoolbar_avatar" in resource_id:
            points.append((x, y))

    for node in snapshot.get("nodes", []):
        center = node.get("center")
        if not center:
            continue
        x, y = int(center[0]), int(center[1])
        if not (0 <= x < width and 0 <= y <= height * 0.14):
            continue
        resource_id = str(node.get("resource_id") or "").strip().lower()
        if "toolbar_content" in resource_id:
            points.append((x, y))

    points.append(_relative_point(screen_size, 0.21, 0.064))

    deduped: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for point in points:
        bucket = (round(point[0] / 12), round(point[1] / 12))
        if bucket in seen:
            continue
        seen.add(bucket)
        deduped.append(point)
    return deduped


def _return_to_bumble_chat_thread(
    device: Any,
    *,
    output_dir: Path,
    label: str,
) -> dict[str, Any]:
    last_snapshot: dict[str, Any] | None = None
    for attempt in range(1, 6):
        snapshot = _collect_visible_text_snapshot(
            device,
            label=label if attempt == 1 else f"{label}_return_attempt_{attempt}",
            output_dir=output_dir,
        )
        last_snapshot = snapshot
        if _looks_like_bumble_chat_thread(
            snapshot["texts"],
            str(snapshot.get("activity") or ""),
        ):
            return snapshot

        activity = str(snapshot.get("activity") or "").lower()
        if "profile" in activity or _looks_like_bumble_profile_surface(snapshot["texts"]):
            clicked = _click_visible_text_fast(device, ("Close",))
            if not clicked:
                device.shell("input", "keyevent", "BACK", timeout=6, check=False)
        else:
            device.shell("input", "keyevent", "BACK", timeout=6, check=False)
        time.sleep(random.uniform(0.5, 0.95))

    if last_snapshot is not None:
        return last_snapshot
    return _collect_visible_text_snapshot(device, label=label, output_dir=output_dir)


def _chat_title_from_snapshot(snapshot: dict[str, Any]) -> str:
    for node in snapshot.get("nodes", []):
        resource_id = str(node.get("resource_id") or "").strip().lower()
        text = str(node.get("text") or "").strip()
        if text and "chattoolbar_title" in resource_id:
            return text
    ignored = {
        "back",
        "view profile",
        "voice call",
        "video call",
        "chats",
        "search",
        "options",
    }
    for value in snapshot.get("texts", []):
        text = str(value).strip()
        if text and text.lower() not in ignored:
            return text
    return ""


def _chat_title_from_candidate_label(label: str) -> str:
    return re.sub(
        r",\s*(date|bff|bizz),\s*match$",
        "",
        str(label or "").strip(),
        flags=re.I,
    ).strip()


def _chat_title_is_generic(title: str) -> bool:
    lower = str(title or "").strip().lower()
    if not lower:
        return True
    if lower in {
        "back",
        "view profile",
        "voice call",
        "video call",
        "chats",
        "search",
        "options",
        "today",
        "yesterday",
        "type a message",
        "send message",
        "reply",
    }:
        return True
    if re.fullmatch(r"[a-z]{3,9} \d{1,2}, \d{4}", lower):
        return True
    return False


def _filter_chat_message_texts(texts: Iterable[str], chat_title: str = "") -> list[str]:
    ignored = {
        "back",
        "view profile",
        "voice call",
        "video call",
        "type a message",
        "send",
        "send message",
        "reply",
        "message...",
        "options",
        "camera",
        "aa",
        "gif",
        "voice message",
        "send message",
        "delivered",
        "read",
        "sent",
        "sending",
        "failed to send",
        "you have 24 hours to reply",
        "it's your turn to message them back",
        "it’s your turn to message them back",
    }
    if chat_title:
        ignored.add(chat_title.strip().lower())
    return [
        text
        for text in _dedupe_strings(texts)
        if text.strip().lower() not in ignored
        and not _is_bumble_system_chat_text(text)
        and not re.fullmatch(r"you have \d+ hours? to reply", text.strip().lower())
    ]


def _is_bumble_system_chat_text(text: str, resource_id: str = "") -> bool:
    lower = text.strip().lower()
    resource_lower = resource_id.strip().lower()
    if not lower and not resource_lower:
        return False
    if any(
        marker in resource_lower
        for marker in (
            "chat_verificationrequest",
            "chat_24_hours_to_reply_banner",
            "placard_",
        )
    ):
        return True
    if lower.startswith("ask ") and " to verify their profile" in lower:
        return True
    if lower in {
        "you have 24 hours to reply",
        "it's your turn to message them back",
        "it’s your turn to message them back",
    }:
        return True
    if re.fullmatch(r"you have \d+ hours? to reply", lower):
        return True
    return False


def _scroll_matched_profile_lower(device: Any, screen_size: tuple[int, int], scroll_idx: int) -> None:
    width, height = screen_size
    start_x = _clamp(round(width * random.uniform(0.44, 0.56)), 80, width - 80)
    end_x = _clamp(start_x + round(random.gauss(0, 38)), 80, width - 80)
    start_y = _clamp(round(height * random.uniform(0.79, 0.87)), 320, height - 220)
    end_y = _clamp(round(height * random.uniform(0.16, 0.26)), 140, height - 520)
    duration_ms = random.randint(760, 1180) + min(scroll_idx, 3) * random.randint(40, 110)

    # Bumble's profile preview is Compose-heavy; one long native swipe is more
    # reliable here than many tiny motion-event segments.
    device.swipe_segment((start_x, start_y), (end_x, end_y), duration_ms)
    time.sleep(random.uniform(0.18, 0.42))


BUMBLE_REPLY_SYSTEM_PROMPT_TEMPLATE = """角色设定：你现在是一个在 Bumble 上互动的真实用户。目标是像个普通、得体且有意思的朋友一样聊天，逐渐建立信任。

核心规则：
1. 语言一致性（极重要）：必须严格匹配对方使用的语言。如果对方发英文，你必须全程使用纯英文回复；如果对方发中文，你才使用中文。严禁混用。
2. 自然得体（像真人）：回复要极度简洁，但不要固定成两句话。多数时候 1 句就够；有时可以只回应、不追问。要像即时聊天，语气自然，少用感叹号。不要表现得像销售、情感专家或客服。
3. 真实聊天质感：可以偶尔使用 emoji、符号表情（如 🙂 / haha / lol / 😂），但不要每条都用。英文可以自然使用 tbh、kinda、rn、lol、haha、yeah、wanna、gonna 等缩写；可以小写开头，标点可省略或略微不完整，不必像正式写作一样全对。必须符合对方语言和语境，不能油腻或装年轻。
4. 句式变化：不要形成固定模式，尤其不要每次都是“回应一句 + 再问一个问题”。reply_parts 默认只放 1 条；只有真的像真人连续补一句时才拆成 2 条，最多 3 条。不要为了推进对话强行问问题。
5. 时空同步（基于对方位置）：
   - 绝对参考时间：{utc_now}（实时变化的 UTC 时间）。
   - 对方所在地：{target_location}（如 Singapore）。
   - 任务：利用地理知识推算出对方当前的当地时间。回复必须符合对方那边的现实情况（晨昏、日期、季节）。
   - 安全机制：如果位置未知，严禁提到“早安/晚安”等时间词。
   - 不要主动提星期几，除非对方明确聊到星期且你能确认和当地日期一致。
6. 拒绝猥琐：严禁任何带有骚扰性、过于亲昵或低俗的语言。保持适当社交礼仪。
7. 循序渐进：前期严禁索要联系方式。只有当热度值 heat_score >= {heat_threshold}，并且对话已有足够互动、对方明显积极时，才可以自然地建议换到更方便的地方继续聊。
8. 联系方式策略：优先让客户留下她常用的联系方式，由人工在后台添加。默认不要主动发送我方号码；只有客户明确问你要你的联系方式时，才可以给出总控里当前允许的联系方式。
9. 平台词限制：回复正文里不要主动写 WhatsApp、WS、WA 这些词，也不要使用缩写、拆写或变体。需要转场时，用自然泛化表达，例如 somewhere easier、usual contact、常用联系方式、换个方便点的地方聊。
10. 外部联系方式边界：如果客户索要 Telegram/TG、LINE、Facebook/FB、Instagram/IG/INS、Snapchat、TikTok、小红书、微博、微信等社交账号，必须委婉表示自己不太玩那些平台，然后用不点名平台的方式转到更方便的联系方式。绝对不能提供、编造或索要任何未配置账号。
11. 女性账号主动开场：如果本账号是女性账号，且当前任务是 new_match_initiate，可以先发送第一条消息；但必须自然、简短、低压力，不要像模板群发。
12. 热度判断：请根据双方资料匹配度、对方回复积极度、问题来回、情绪投入、是否主动延展话题，给出 0-100 的 heat_score。
13. 输出格式：必须只输出严格 JSON，不要 Markdown，不要解释。JSON 字段：reply, reply_parts, heat_score, should_ask_ws, reason。reply 是真正要发送的正文；reply_parts 是可选数组，用于拆分多条消息；如果不该回复，reply 为空字符串且 reply_parts 为空数组。
"""


def _extract_conversation_events(snapshot: dict[str, Any], *, screen_size: tuple[int, int]) -> list[dict[str, Any]]:
    width, _height = screen_size
    ignored_exact = {
        "back",
        "view profile",
        "voice call",
        "video call",
        "options",
        "camera",
        "aa",
        "gif",
        "voice message",
        "send message",
        "reply",
        "delivered",
        "read",
        "sent",
        "sending",
        "failed to send",
        "you have 24 hours to reply",
        "it's your turn to message them back",
        "it’s your turn to message them back",
    }
    events: list[dict[str, Any]] = []
    current_date = ""
    for node in snapshot.get("nodes", []):
        text = str(node.get("text") or node.get("content_desc") or "").strip()
        if not text:
            continue
        lower = text.lower()
        resource_id = str(node.get("resource_id") or "").lower()
        class_name = str(node.get("class") or "").lower()
        center = node.get("center")
        if not center:
            continue
        x, y = int(center[0]), int(center[1])
        if lower in ignored_exact:
            continue
        if _is_bumble_system_chat_text(text, resource_id):
            continue
        if re.fullmatch(r"you have \d+ hours? to reply", lower):
            continue
        if "chatinput" in resource_id or "edittext" in class_name:
            continue
        if "chattoolbar_title" in resource_id:
            continue
        if "timestamp_text" in resource_id or re.fullmatch(r"[A-Z][a-z]{2,8} \d{1,2}, \d{4}", text):
            current_date = text
            events.append({"type": "date", "text": text, "center": [x, y]})
            continue
        if y < 240:
            continue
        sender = "customer" if x < width * 0.5 else "self"
        events.append(
            {
                "type": "message",
                "sender": sender,
                "text": text,
                "date": current_date,
                "center": [x, y],
            }
        )
    return events


def _conversation_events_to_text(events: Iterable[dict[str, Any]]) -> str:
    lines: list[str] = []
    for event in events:
        if event.get("type") == "date":
            lines.append(str(event.get("text") or ""))
        elif event.get("type") == "message":
            sender = "Customer" if event.get("sender") == "customer" else "Me"
            lines.append(f"{sender}: {event.get('text')}")
    return "\n".join(line for line in lines if line.strip())


def _last_message_event(events: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    last: dict[str, Any] | None = None
    for event in events:
        if event.get("type") == "message" and str(event.get("text") or "").strip():
            last = event
    return last


def _chat_reply_state_key(profile_id: str, chat_title: str, last_event: dict[str, Any] | None) -> str:
    last_text = str((last_event or {}).get("text") or "")
    raw = f"{profile_id}|{chat_title}|{last_text}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _load_reply_state(config: Config) -> dict[str, Any]:
    state_path = config.STATE_FILE.with_name(config.STATE_FILE.stem + "_bumble_replies.json")
    if not state_path.exists():
        return {"path": str(state_path), "sent": {}}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"path": str(state_path), "sent": {}}
    if not isinstance(data, dict):
        return {"path": str(state_path), "sent": {}}
    data.setdefault("path", str(state_path))
    data.setdefault("sent", {})
    return data


def _save_reply_state(state: dict[str, Any]) -> None:
    path = Path(str(state.get("path") or ""))
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with lock_path.open("w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        current: dict[str, Any] = {"path": str(path), "sent": {}}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    current.update(loaded)
            except (OSError, ValueError):
                pass
        current["path"] = str(path)
        current_sent = current.setdefault("sent", {})
        if not isinstance(current_sent, dict):
            current_sent = {}
            current["sent"] = current_sent
        incoming_sent = state.get("sent") or {}
        if isinstance(incoming_sent, dict):
            current_sent.update(incoming_sent)
        tmp_path = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
        tmp_path.write_text(
            json.dumps(current, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(path)


def _append_reply_monitor_event(path: Path, event: dict[str, Any]) -> None:
    payload = {
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        **event,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _reply_candidate_key(label: str, point: Any) -> str:
    try:
        x = int(point[0])
        y = int(point[1])
    except (TypeError, ValueError, IndexError):
        x = 0
        y = 0
    return f"{label.strip().lower()}|{x}|{y}"


def _candidate_row_needs_reply(candidate: dict[str, Any]) -> bool:
    row_context = "\n".join(
        str(value or "").strip().lower()
        for value in candidate.get("row_context") or []
        if str(value or "").strip()
    )
    resource_id = str(candidate.get("resource_id") or "").strip().lower()
    return "your move" in row_context or "badgeunread" in resource_id


def _candidate_is_recent_chat_row(candidate: dict[str, Any]) -> bool:
    resource_id = str(candidate.get("resource_id") or "").strip().lower()
    if not any(
        marker in resource_id
        for marker in ("connectionsitem_personname", "connectionsitem_message")
    ):
        return False
    row_context = "\n".join(
        str(value or "").strip().lower()
        for value in candidate.get("row_context") or []
        if str(value or "").strip()
    )
    excluded_markers = (
        "your opening moves",
        "people like you",
        "view who likes you",
        "conversation expired",
        "expired match",
    )
    return not any(marker in row_context for marker in excluded_markers)


def _normalize_chat_candidate_name(value: str) -> str:
    normalized = re.sub(r"\s+", " ", str(value or "").strip().lower())
    normalized = re.sub(r",\s*(date|bff|bizz),\s*match$", "", normalized)
    return normalized.strip()


def _chat_candidate_matches_label(candidate: dict[str, Any], wanted_label: str) -> bool:
    wanted = _normalize_chat_candidate_name(wanted_label)
    if not wanted:
        return False
    candidate_values = [
        str(candidate.get("label") or ""),
        *[str(value or "") for value in candidate.get("row_context") or []],
    ]
    return any(_normalize_chat_candidate_name(value) == wanted for value in candidate_values)


def _build_reply_audit_entry(index: int, candidate: dict[str, Any]) -> dict[str, Any]:
    point = candidate.get("click_center") or candidate.get("node_center") or [0, 0]
    label = str(candidate.get("label") or "").strip()
    return {
        "candidate_index": index,
        "key": _reply_candidate_key(label, point),
        "label": label,
        "point": [int(point[0]), int(point[1])] if isinstance(point, (list, tuple)) and len(point) >= 2 else [0, 0],
        "row_context": list(candidate.get("row_context") or []),
        "resource_id": candidate.get("resource_id"),
        "needs_reply_from_list": _candidate_row_needs_reply(candidate),
        "status": "pending_not_opened",
        "reason": "",
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }


def _write_reply_audit_files(output_dir: Path, audit: list[dict[str, Any]]) -> dict[str, str]:
    json_path = output_dir / "reply_audit.json"
    text_path = output_dir / "reply_audit.txt"
    json_path.write_text(
        json.dumps(audit, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    lines: list[str] = []
    for item in audit:
        marker = "NEEDS_REPLY" if item.get("needs_reply_from_list") else "NO_LIST_NEED"
        status = str(item.get("status") or "")
        label = str(item.get("label") or "")
        row_context = " | ".join(str(value) for value in item.get("row_context") or [])
        reason = str(item.get("reason") or "")
        last_sender = str(item.get("last_message_sender") or "")
        last_text = str(item.get("last_message_text") or "")
        reply_text = str(item.get("reply_text") or "")
        lines.append(f"[{marker}] {label} :: {status}")
        if row_context:
            lines.append(f"  row: {row_context}")
        if last_sender or last_text:
            lines.append(f"  last: {last_sender}: {last_text}".rstrip())
        if reply_text:
            lines.append(f"  reply: {reply_text}")
        if reason:
            lines.append(f"  reason: {reason}")
    text_path.write_text("\n".join(lines).strip() + ("\n" if lines else ""), encoding="utf-8")
    return {"json": str(json_path), "txt": str(text_path)}


def _reply_audit_needs_attention(audit: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        item
        for item in audit
        if bool(item.get("needs_reply_from_list")) and str(item.get("status") or "") != "sent"
    ]


def _find_latest_self_profile_text() -> tuple[str, str]:
    candidates = sorted(Path.cwd().glob("diagnostics/**/self_profile/texts.txt"))
    for path in reversed(candidates):
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            return text, str(path)
    return "", ""


def _find_self_profile_text_for_phone(profile_id: str) -> tuple[str, str]:
    if not profile_id:
        return "", ""
    candidates = sorted(Path.cwd().glob(f"diagnostics/**/{profile_id}/**/self_profile/texts.txt"))
    candidates.extend(sorted(Path.cwd().glob(f"diagnostics/**/*{profile_id}*/self_profile/texts.txt")))
    seen: set[Path] = set()
    for path in reversed(candidates):
        if path in seen:
            continue
        seen.add(path)
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            return text, str(path)
    return "", ""


def _image_file_to_ai_data_url(image_path: Path) -> tuple[str, dict[str, Any]]:
    raw = image_path.read_bytes()
    mime = "image/png"
    metadata: dict[str, Any] = {
        "source_path": str(image_path),
        "source_bytes": len(raw),
        "mime": mime,
        "compressed": False,
    }

    try:
        from PIL import Image

        with Image.open(io.BytesIO(raw)) as image:
            original_size = image.size
            converted = image.convert("RGB")
            converted.thumbnail((720, 720))
            buffer = io.BytesIO()
            converted.save(buffer, format="JPEG", quality=72, optimize=True)
            compressed = buffer.getvalue()
            if compressed and len(compressed) < len(raw):
                raw = compressed
                mime = "image/jpeg"
                metadata.update(
                    {
                        "mime": mime,
                        "compressed": True,
                        "original_size": list(original_size),
                        "encoded_size": list(converted.size),
                        "encoded_bytes": len(raw),
                    }
                )
    except Exception as exc:
        LOGGER.debug("AI image compression fallback to original PNG for %s: %s", image_path, exc)

    metadata.setdefault("encoded_bytes", len(raw))
    data_url = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
    return data_url, metadata


def _normalize_ai_json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    text = re.sub(r"^```(?:json)?", "", text, flags=re.I).strip()
    text = re.sub(r"```$", "", text).strip()
    try:
        data = json.loads(text)
    except ValueError as exc:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            raise AIAPIError(f"AI did not return JSON: {text[:160]}") from exc
        data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise AIAPIError("AI JSON response was not an object.")
    reply = str(data.get("reply") or "").strip()
    parts_raw = data.get("reply_parts")
    if isinstance(parts_raw, list):
        parts = [str(part).strip() for part in parts_raw if str(part).strip()]
    else:
        parts = []
    if not parts and reply:
        parts = [reply]
    parts = parts[:3]
    heat_score = int(float(data.get("heat_score") or 0))
    return {
        "reply": reply,
        "reply_parts": parts,
        "heat_score": _clamp(heat_score, 0, 100),
        "should_ask_ws": bool(data.get("should_ask_ws", False)),
        "reason": str(data.get("reason") or ""),
        "raw": data,
    }


def _timezone_name_for_target_location(target_location: str) -> str | None:
    location = (target_location or "").strip().lower()
    if not location or location == "unknown":
        return None
    mapping = {
        "singapore": "Asia/Singapore",
        "sg": "Asia/Singapore",
        "hong kong": "Asia/Hong_Kong",
        "hongkong": "Asia/Hong_Kong",
        "hk": "Asia/Hong_Kong",
        "tokyo": "Asia/Tokyo",
        "japan": "Asia/Tokyo",
        "osaka": "Asia/Tokyo",
        "seoul": "Asia/Seoul",
        "korea": "Asia/Seoul",
        "taiwan": "Asia/Taipei",
        "taipei": "Asia/Taipei",
        "manila": "Asia/Manila",
        "philippines": "Asia/Manila",
        "bangkok": "Asia/Bangkok",
        "thailand": "Asia/Bangkok",
        "kuala lumpur": "Asia/Kuala_Lumpur",
        "malaysia": "Asia/Kuala_Lumpur",
        "jakarta": "Asia/Jakarta",
        "indonesia": "Asia/Jakarta",
    }
    for marker, tz_name in mapping.items():
        if marker in location:
            return tz_name
    return None


def _validate_bumble_ai_temporal_reply(
    ai_result: dict[str, Any],
    *,
    target_location: str,
    utc_now: dt.datetime,
) -> None:
    reply_text = " ".join(
        str(value or "")
        for value in [ai_result.get("reply"), *(ai_result.get("reply_parts") or [])]
    ).strip()
    if not reply_text:
        return
    lowered = reply_text.lower()
    weekday_names = (
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    )
    mentioned_weekdays = [
        day for day in weekday_names if re.search(rf"\b{day}\b", lowered)
    ]
    tz_name = _timezone_name_for_target_location(target_location)
    if mentioned_weekdays:
        if not tz_name or ZoneInfo is None:
            raise AIAPIError(
                "AI reply mentioned a weekday while target timezone could not be verified."
            )
        local_now = utc_now.astimezone(ZoneInfo(tz_name))
        actual_weekday = local_now.strftime("%A").lower()
        wrong_weekdays = [day for day in mentioned_weekdays if day != actual_weekday]
        if wrong_weekdays:
            raise AIAPIError(
                "AI reply mentioned an incorrect weekday for "
                f"{target_location}: {wrong_weekdays}; actual={actual_weekday}."
            )

    if not tz_name:
        unsafe_time_terms = (
            r"\bgood morning\b",
            r"\bgood night\b",
            r"\bmorning\b",
            r"\bafternoon\b",
            r"\bevening\b",
            r"\btonight\b",
            r"\btonite\b",
            r"早安",
            r"早上",
            r"上午",
            r"下午",
            r"晚上",
            r"晚安",
        )
        if any(re.search(pattern, lowered) for pattern in unsafe_time_terms):
            raise AIAPIError(
                "AI reply used time-of-day wording while target location is unknown."
            )


def _prepare_reply_for_adb_text(text: str) -> str:
    return _normalize_text_for_device_input(text, allow_unicode=False)


def _prepare_reply_for_device_text(text: str, *, allow_unicode: bool) -> str:
    return _normalize_text_for_device_input(text, allow_unicode=allow_unicode)


def _has_non_ascii_letter_or_number(text: str) -> bool:
    return any(
        ord(ch) > 127 and unicodedata.category(ch)[:1] in {"L", "N"}
        for ch in text
    )


def _ascii_fallback_reply_for_non_ascii_message(text: str) -> str:
    lowered = text.strip().lower()
    if any("\u4e00" <= ch <= "\u9fff" for ch in text):
        if "看见" in text or "見" in text:
            return "haha i see you too"
        return "haha i get you"
    if any(ord(ch) > 127 for ch in text):
        return "haha"
    if "?" in lowered:
        return "haha good question"
    return "haha i get you"


def _find_chat_input_point(snapshot: dict[str, Any], screen_size: tuple[int, int]) -> tuple[int, int]:
    for node in snapshot.get("nodes", []):
        resource_id = str(node.get("resource_id") or "").lower()
        class_name = str(node.get("class") or "").lower()
        center = node.get("center")
        if center and ("chatinput_text" in resource_id or "edittext" in class_name):
            return int(center[0]), int(center[1])
    return _relative_point(screen_size, 0.45, 0.912)


def _has_chat_input_node(snapshot: dict[str, Any]) -> bool:
    for node in snapshot.get("nodes", []):
        resource_id = str(node.get("resource_id") or "").lower()
        class_name = str(node.get("class") or "").lower()
        if "chatinput_text" in resource_id or "edittext" in class_name:
            return True
    return False


def _find_opening_move_reply_point(snapshot: dict[str, Any]) -> tuple[int, int] | None:
    best: tuple[int, tuple[int, int]] | None = None
    for node in snapshot.get("nodes", []):
        text = str(node.get("text") or node.get("content_desc") or "").strip().lower()
        center = node.get("center")
        if not center or text != "reply":
            continue
        x, y = int(center[0]), int(center[1])
        priority = 0 if y > 400 else 1
        if best is None or priority < best[0]:
            best = (priority, (x, y))
    return best[1] if best is not None else None


def _bounds_rect(bounds: str) -> tuple[int, int, int, int] | None:
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
    if not match:
        return None
    left, top, right, bottom = (int(part) for part in match.groups())
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _node_touch_points(node: dict[str, Any], screen_size: tuple[int, int]) -> list[tuple[int, int]]:
    width, height = screen_size
    points: list[tuple[int, int]] = []
    center = node.get("center")
    if center:
        points.append((int(center[0]), int(center[1])))
    rect = _bounds_rect(str(node.get("bounds") or ""))
    if rect:
        left, top, right, bottom = rect
        inner_left = _clamp(left + max(4, round((right - left) * 0.28)), 1, width - 2)
        inner_right = _clamp(right - max(4, round((right - left) * 0.28)), 1, width - 2)
        inner_top = _clamp(top + max(4, round((bottom - top) * 0.28)), 1, height - 2)
        inner_bottom = _clamp(bottom - max(4, round((bottom - top) * 0.28)), 1, height - 2)
        mid_x = _clamp(round((left + right) / 2), 1, width - 2)
        mid_y = _clamp(round((top + bottom) / 2), 1, height - 2)
        points.extend(
            [
                (mid_x, mid_y),
                (inner_right, mid_y),
                (inner_left, mid_y),
                (mid_x, inner_top),
                (mid_x, inner_bottom),
            ]
        )
    deduped: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for x, y in points:
        point = (_clamp(int(x), 1, width - 2), _clamp(int(y), 1, height - 2))
        bucket = (round(point[0] / 6), round(point[1] / 6))
        if bucket in seen:
            continue
        seen.add(bucket)
        deduped.append(point)
    return deduped


def _find_chat_send_points(snapshot: dict[str, Any], screen_size: tuple[int, int]) -> list[tuple[int, int]]:
    candidates: list[tuple[int, int, tuple[int, int]]] = []
    for node in snapshot.get("nodes", []):
        center = node.get("center")
        if not center:
            continue
        x, y = int(center[0]), int(center[1])
        resource_id = str(node.get("resource_id") or "").lower()
        content_desc = str(node.get("content_desc") or "").lower()
        if y < screen_size[1] * 0.82:
            continue
        if "send" in content_desc or "send" in resource_id:
            priority = 0 if bool(node.get("enabled", True)) else 1
            for point in _node_touch_points(node, screen_size):
                candidates.append((priority, -point[0], point))
            continue
        if any(marker in resource_id for marker in ("right_extra", "recording_iconcomponent")):
            for point in _node_touch_points(node, screen_size):
                candidates.append((2, -point[0], point))
    fallback = _relative_point(screen_size, 0.945, 0.912)
    candidates.append((9, -fallback[0], fallback))
    candidates.sort(key=lambda item: (item[0], item[1]))

    points: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for _priority, _negative_x, point in candidates:
        bucket = (round(point[0] / 8), round(point[1] / 8))
        if bucket in seen:
            continue
        seen.add(bucket)
        points.append(point)
        if len(points) >= 8:
            break
    return points


def _chat_input_text_from_snapshot(snapshot: dict[str, Any]) -> str:
    for node in snapshot.get("nodes", []):
        resource_id = str(node.get("resource_id") or "").lower()
        class_name = str(node.get("class") or "").lower()
        if "chatinput_text" in resource_id or "edittext" in class_name:
            value = str(node.get("text") or "").strip()
            if value.lower() in {"aa", "type a message", "send a message", "message..."}:
                return ""
            return value
    return ""


def _snapshot_has_text_outside_chat_input(snapshot: dict[str, Any], text: str) -> bool:
    needle = re.sub(r"\s+", " ", text).strip().lower()
    if not needle:
        return False
    for node in snapshot.get("nodes", []):
        resource_id = str(node.get("resource_id") or "").lower()
        class_name = str(node.get("class") or "").lower()
        if "chatinput_text" in resource_id or "edittext" in class_name:
            continue
        value = re.sub(r"\s+", " ", str(node.get("text") or "")).strip().lower()
        if needle and needle in value:
            return True
    return False


def _clear_chat_input_text(
    device: Any,
    *,
    snapshot: dict[str, Any],
    screen_size: tuple[int, int],
    chat_dir: Path,
    label: str,
) -> dict[str, Any]:
    existing = _chat_input_text_from_snapshot(snapshot)
    if not existing:
        return snapshot
    LOGGER.warning("Clearing existing chat draft before reply: %s", existing[:120])
    input_point = _find_chat_input_point(snapshot, screen_size)
    human_gaussian_click(device, input_point[0], input_point[1], sigma_px=5.0, max_offset_px=12)
    time.sleep(random.uniform(0.25, 0.55))
    delete_count = 360
    script = (
        "input keyevent KEYCODE_MOVE_END; "
        f"i=0; while [ $i -lt {delete_count} ]; do input keyevent DEL; i=$((i+1)); done"
    )
    device.shell("sh", "-c", script, timeout=45.0, check=False)
    time.sleep(random.uniform(0.35, 0.75))
    return _collect_visible_text_snapshot(
        device,
        label=label,
        output_dir=chat_dir,
    )


def _tap_send_control(device: Any, point: tuple[int, int], strategy: str) -> None:
    x, y = int(point[0]), int(point[1])
    if strategy == "tap":
        device.tap(x, y)
        return
    if strategy == "touchscreen_tap":
        device.shell("input", "touchscreen", "tap", x, y, timeout=8, check=False)
        return
    if strategy == "short_swipe":
        device.shell("input", "swipe", x, y, x, y, 70, timeout=8, check=False)
        return
    if strategy == "motion_event":
        device.motion_event("DOWN", x, y)
        time.sleep(random.uniform(0.045, 0.09))
        device.motion_event("UP", x, y)
        return
    raise ValueError(f"Unknown send tap strategy: {strategy}")


def _call_bumble_reply_ai(
    config: Config,
    *,
    self_profile_text: str,
    customer_profile_text: str,
    conversation_text: str,
    chat_title: str,
    image_path: Path | None,
    target_location: str = "unknown",
    heat_threshold: int = 70,
    output_dir: Path,
) -> dict[str, Any]:
    utc_now_dt = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    utc_now = utc_now_dt.isoformat()
    system_prompt = BUMBLE_REPLY_SYSTEM_PROMPT_TEMPLATE.format(
        utc_now=utc_now,
        target_location=target_location or "unknown",
        heat_threshold=heat_threshold,
    )
    context = {
        "task": "reply_to_existing_bumble_chat",
        "utc_now": utc_now,
        "target_location": target_location or "unknown",
        "heat_threshold": heat_threshold,
        "account_owner_profile": self_profile_text,
        "customer_name": chat_title,
        "customer_profile": customer_profile_text,
        "chat_history": conversation_text,
        "state": {
            "platform": "Bumble",
            "current_task": "reply_to_existing_chat",
            "configured_external_contact": None,
        },
        "automation_constraints": {
            "unicode_input_supported": bool(config.USE_ADB_KEYBOARD),
            "reply_must_be_ascii_safe": not bool(config.USE_ADB_KEYBOARD),
            "do_not_mention_ai": True,
        },
    }

    user_content: list[dict[str, Any]] = [
        {"type": "text", "text": json.dumps(context, ensure_ascii=False)}
    ]
    if image_path and image_path.exists():
        try:
            image_data_url, image_metadata = _image_file_to_ai_data_url(image_path)
            (output_dir / "ai_image_context.json").write_text(
                json.dumps(image_metadata, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            user_content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": image_data_url},
                }
            )
        except OSError:
            LOGGER.debug("Could not read customer image for AI: %s", image_path, exc_info=True)

    payload = {
        "model": config.AI_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.65,
        "top_p": 0.9,
        "max_tokens": config.AI_REPLY_MAX_TOKENS,
        "response_format": {"type": "json_object"},
    }

    (output_dir / "ai_input_context.json").write_text(
        json.dumps(context, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    url = f"{config.AI_BASE_URL}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.AI_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    last_error: Exception | None = None
    for attempt in range(config.AI_MAX_RETRIES + 1):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=config.AI_TIMEOUT_SECONDS)
            response.raise_for_status()
            data = response.json()
            (output_dir / "ai_response.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            choice = data["choices"][0]
            content = str(choice["message"]["content"])
            if choice.get("finish_reason") == "length":
                raise AIAPIError(
                    "AI response was truncated before complete JSON; "
                    f"content_prefix={content[:80]!r}"
                )
            parsed = _normalize_ai_json(content)
            _validate_bumble_ai_temporal_reply(
                parsed,
                target_location=target_location,
                utc_now=utc_now_dt,
            )
            (output_dir / "ai_reply.json").write_text(
                json.dumps(parsed, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return parsed
        except Exception as exc:
            last_error = exc
            LOGGER.warning("Bumble reply AI call failed on attempt %s: %s", attempt + 1, exc)
            if attempt < config.AI_MAX_RETRIES:
                time.sleep(1.0 + attempt * 0.8 + random.uniform(0.2, 0.7))
    raise AIAPIError(f"Bumble reply AI failed: {last_error}") from last_error


def _send_bumble_reply_parts(
    device: Any,
    *,
    screen_size: tuple[int, int],
    reply_parts: list[str],
    chat_dir: Path,
) -> dict[str, Any]:
    sent_parts: list[str] = []
    send_attempts: list[dict[str, Any]] = []
    device_config = getattr(device, "config", None)
    allow_unicode_input = bool(getattr(device_config, "USE_ADB_KEYBOARD", False))
    for idx, part in enumerate(reply_parts, start=1):
        safe_part = _prepare_reply_for_device_text(part, allow_unicode=allow_unicode_input)
        if not safe_part:
            continue
        before = _collect_visible_text_snapshot(
            device,
            label=f"reply_part_{idx}_before",
            output_dir=chat_dir,
        )
        if not _has_chat_input_node(before):
            opening_reply_point = _find_opening_move_reply_point(before)
            if opening_reply_point is not None:
                LOGGER.info("Opening Bumble Opening Move reply composer at %s.", opening_reply_point)
                human_gaussian_click(
                    device,
                    opening_reply_point[0],
                    opening_reply_point[1],
                    sigma_px=5.0,
                    max_offset_px=14,
                )
                time.sleep(random.uniform(0.9, 1.45))
                before = _collect_visible_text_snapshot(
                    device,
                    label=f"reply_part_{idx}_after_opening_move_reply_tap",
                    output_dir=chat_dir,
                )
            if not _has_chat_input_node(before):
                raise ADBCommandError("Chat input was not visible before typing reply.")
        input_point = _find_chat_input_point(before, screen_size)
        human_gaussian_click(device, input_point[0], input_point[1], sigma_px=7.0, max_offset_px=18)
        time.sleep(random.uniform(0.35, 0.75))
        before_input_text = _chat_input_text_from_snapshot(before)
        if before_input_text:
            before = _clear_chat_input_text(
                device,
                snapshot=before,
                screen_size=screen_size,
                chat_dir=chat_dir,
                label=f"reply_part_{idx}_cleared_existing_input",
            )
            before_input_text = _chat_input_text_from_snapshot(before)
            if before_input_text:
                before = _clear_chat_input_text(
                    device,
                    snapshot=before,
                    screen_size=screen_size,
                    chat_dir=chat_dir,
                    label=f"reply_part_{idx}_cleared_existing_input_retry",
                )
                before_input_text = _chat_input_text_from_snapshot(before)
            if before_input_text:
                raise ADBCommandError(
                    f"Chat input was not empty before typing reply part {idx}: {before_input_text[:80]}"
                )
        device.input_text(safe_part)
        time.sleep(random.uniform(0.55, 1.05))
        typed = _collect_visible_text_snapshot(
            device,
            label=f"reply_part_{idx}_typed",
            output_dir=chat_dir,
        )
        typed_input_text = _chat_input_text_from_snapshot(typed)
        if safe_part.lower() not in typed_input_text.lower():
            raise ADBCommandError(
                f"Reply part {idx} was not visible in the chat input after typing."
            )
        device.shell("input", "keyevent", "BACK", timeout=6, check=False)
        time.sleep(random.uniform(0.65, 1.05))
        unfocused = _collect_visible_text_snapshot(
            device,
            label=f"reply_part_{idx}_keyboard_hidden_before_send",
            output_dir=chat_dir,
        )
        unfocused_input_text = _chat_input_text_from_snapshot(unfocused)
        if safe_part.lower() in unfocused_input_text.lower():
            typed = unfocused
            typed_input_text = unfocused_input_text
        elif not _looks_like_bumble_chat_thread(unfocused.get("texts", []), unfocused.get("activity", "")):
            raise ADBCommandError("Chat input lost focus by leaving the thread before send.")
        after = typed
        sent_ok = False
        outside_visible = False
        input_after_send = typed_input_text
        send_strategies = ("tap", "touchscreen_tap", "short_swipe", "motion_event")
        send_attempt = 0
        for point in _find_chat_send_points(typed, screen_size):
            for strategy in send_strategies:
                send_attempt += 1
                # Use exact taps for the send button; a Gaussian offset is useful
                # for browsing gestures but can miss compact Compose buttons.
                _tap_send_control(device, point, strategy)
                time.sleep(random.uniform(0.9, 1.45) + 0.12 * min(send_attempt, 4))
                after = _collect_visible_text_snapshot(
                    device,
                    label=f"reply_part_{idx}_after_send_{send_attempt}",
                    output_dir=chat_dir,
                )
                input_after_send = _chat_input_text_from_snapshot(after)
                outside_visible = _snapshot_has_text_outside_chat_input(after, safe_part)
                send_attempts.append(
                    {
                        "part_index": idx,
                        "attempt": send_attempt,
                        "strategy": strategy,
                        "send_point": point,
                        "input_after_send": input_after_send,
                        "outside_visible": outside_visible,
                    }
                )
                if not input_after_send or safe_part.lower() not in input_after_send.lower():
                    sent_ok = True
                    break
            if sent_ok:
                break

        if not sent_ok:
            device.shell("input", "keyevent", "BACK", timeout=6, check=False)
            time.sleep(random.uniform(0.7, 1.15))
            after = _collect_visible_text_snapshot(
                device,
                label=f"reply_part_{idx}_keyboard_hidden",
                output_dir=chat_dir,
            )
            input_after_send = _chat_input_text_from_snapshot(after)
            outside_visible = _snapshot_has_text_outside_chat_input(after, safe_part)
            send_attempts.append(
                {
                    "part_index": idx,
                    "attempt": "hide_keyboard",
                    "strategy": "back",
                    "send_point": "keyboard",
                    "input_after_send": input_after_send,
                    "outside_visible": outside_visible,
                }
            )
            if input_after_send and safe_part.lower() in input_after_send.lower():
                for point in _find_chat_send_points(after, screen_size)[:4]:
                    for strategy in send_strategies:
                        send_attempt += 1
                        _tap_send_control(device, point, strategy)
                        time.sleep(random.uniform(0.9, 1.45) + 0.12 * min(send_attempt, 4))
                        after = _collect_visible_text_snapshot(
                            device,
                            label=f"reply_part_{idx}_after_hidden_send_{send_attempt}",
                            output_dir=chat_dir,
                        )
                        input_after_send = _chat_input_text_from_snapshot(after)
                        outside_visible = _snapshot_has_text_outside_chat_input(after, safe_part)
                        send_attempts.append(
                            {
                                "part_index": idx,
                                "attempt": send_attempt,
                                "strategy": f"keyboard_hidden_{strategy}",
                                "send_point": point,
                                "input_after_send": input_after_send,
                                "outside_visible": outside_visible,
                            }
                        )
                        if not input_after_send or safe_part.lower() not in input_after_send.lower():
                            sent_ok = True
                            break
                    if sent_ok:
                        break
        if not sent_ok:
            for key_name in ("KEYCODE_ENTER", "KEYCODE_NUMPAD_ENTER", "ENTER", "NUMPAD_ENTER"):
                device.shell("input", "keyevent", key_name, timeout=8, check=False)
                time.sleep(random.uniform(1.0, 1.6))
                after = _collect_visible_text_snapshot(
                    device,
                    label=f"reply_part_{idx}_after_send_{key_name.lower()}",
                    output_dir=chat_dir,
                )
                input_after_send = _chat_input_text_from_snapshot(after)
                outside_visible = _snapshot_has_text_outside_chat_input(after, safe_part)
                send_attempts.append(
                    {
                        "part_index": idx,
                        "attempt": key_name,
                        "strategy": "keyboard_action",
                        "send_point": "keyboard",
                        "input_after_send": input_after_send,
                        "outside_visible": outside_visible,
                    }
                )
                if not input_after_send or safe_part.lower() not in input_after_send.lower():
                    sent_ok = True
                    break
        if not sent_ok:
            raise ADBCommandError(
                f"Reply part {idx} remained in the chat input after send taps: "
                f"{input_after_send[:120]}"
            )
        if not outside_visible:
            LOGGER.warning("Sent reply part cleared input but was not visible immediately: %s", safe_part)
        sent_parts.append(safe_part)
        if idx < len(reply_parts):
            time.sleep(random.uniform(1.0, 2.4))

    final_snapshot = _collect_visible_text_snapshot(
        device,
        label="reply_final_state",
        output_dir=chat_dir,
    )
    final_input_text = _chat_input_text_from_snapshot(final_snapshot)
    verified = (
        bool(sent_parts)
        and not final_input_text
        and all(_snapshot_has_text_outside_chat_input(final_snapshot, part) for part in sent_parts)
    )
    return {
        "sent_parts": sent_parts,
        "verified_visible": verified,
        "final_input_text": final_input_text,
        "final_text_count": len(final_snapshot.get("texts", [])),
        "send_attempts": send_attempts,
    }


def _read_ui_dump_text_fast(device: GeelarkOpenAPIShellDevice, dump_path: str) -> str:
    parts = [
        device._execute(f"grep -o 'text=\"[^\"]*\"' {dump_path}", check=False),
        device._execute(f"grep -o 'content-desc=\"[^\"]*\"' {dump_path}", check=False),
    ]
    raw = "\n".join(part for part in parts if part.strip())
    if raw.strip():
        return raw

    raw = device._execute(f"cat {dump_path}", check=False)
    if raw.strip():
        return raw

    try:
        return device.dump_ui_xml()
    except (ADBCommandError, UIExtractionError):
        return ""


def _parse_text_values_from_ui_dump(raw: str) -> list[str]:
    if not raw:
        return []

    values = [
        html.unescape(match).strip()
        for match in re.findall(r'(?:text|content-desc)="([^"]*)"', raw)
    ]
    if values:
        return values

    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []

    parsed: list[str] = []
    for element in root.iter():
        text = (element.attrib.get("text") or "").strip()
        desc = (element.attrib.get("content-desc") or "").strip()
        if text:
            parsed.append(html.unescape(text))
        if desc:
            parsed.append(html.unescape(desc))
    return parsed


def run_bumble_right_swipe_loop(
    config: Config,
    *,
    profile_id: str,
    max_count: int | None,
    prepare_adb: bool = False,
    use_api_shell: bool = False,
    start_tab: str = "people",
    output_root: Path | None = None,
    skip_capture: bool = False,
    view_profile_probability: float | None = None,
    view_profile_max_swipes: int | None = None,
    until_daily_limit: bool = False,
    popup_check_every_n_actions: int | None = None,
    stop_phone_after_run: bool = False,
    capture_self_profile: bool = False,
    phone_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Perform randomized Bumble right swipes, with optional profile capture."""

    if max_count is not None and max_count <= 0:
        raise AutomationError("--max-count must be greater than 0.")
    if max_count is None and not until_daily_limit:
        raise AutomationError("Provide --max-count or --until-daily-limit.")

    config = replace(
        config,
        GEELARK_PROFILE_ID=profile_id,
        AUTO_START_PHONE=prepare_adb,
        AUTO_ENABLE_ADB=prepare_adb,
        LOOP_FOREVER=False,
    )
    limiter = DailyActionLimiter(config, scope_id=profile_id)
    device, appium = _build_device_for_profile(config, use_api_shell=use_api_shell)
    output_dir = (
        output_root
        if output_root is not None
        else Path.cwd() / "diagnostics" / profile_id / "right_swipe_run" / _timestamp_for_path()
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    crop_bounds = parse_crop_bounds(config.CUSTOMER_PHOTO_CROP_BOUNDS)
    run_log = output_dir / "run_log.jsonl"
    run_summary = output_dir / "run_summary.json"
    popup_check_every_n_actions = (
        config.POPUP_CHECK_EVERY_N_ACTIONS
        if popup_check_every_n_actions is None
        else popup_check_every_n_actions
    )
    summary_result: dict[str, Any] = {
        "profile_id": profile_id,
        "output_dir": str(output_dir),
        "target_likes": 0,
        "successful_likes": 0,
        "iteration_errors": 0,
        "popup_actions": 0,
        "daily_count": limiter.current_count(),
        "daily_limit": config.DAILY_ACTION_LIMIT,
        "skip_capture": skip_capture,
        "status": "not_started",
    }

    try:
        device.ensure_connected()
        appium.connect()
        self_profile_summary = None
        if capture_self_profile:
            self_profile_summary = save_bumble_self_profile(
                device,
                appium,
                output_dir=output_dir,
                phone_metadata=phone_metadata,
            )
            summary_result["self_profile"] = {
                "saved": True,
                "path": str(output_dir / "self_profile"),
                "text_count": len(self_profile_summary.get("texts", [])),
            }
        open_bumble_tab(device, start_tab)
        startup_popups = handle_common_popups(device)
        if startup_popups:
            LOGGER.info("Handled %s startup popup/action(s).", len(startup_popups))

        screen_size = device.get_screen_size()
        planned_count = limiter.remaining() if until_daily_limit else int(max_count or 0)
        if max_count is not None and until_daily_limit:
            planned_count = min(planned_count, max_count)
        if planned_count <= 0:
            raise DailyLimitExceeded(
                f"Daily limit reached: {limiter.current_count()}/{config.DAILY_ACTION_LIMIT}"
            )
        LOGGER.info(
            "Right-swipe run target=%s; current daily count %s/%s.",
            planned_count,
            limiter.current_count(),
            config.DAILY_ACTION_LIMIT,
        )
        _write_run_summary(
            run_summary,
            profile_id=profile_id,
            output_dir=output_dir,
            target_likes=planned_count,
            successful_likes=0,
            iteration_errors=0,
            popup_actions=len(startup_popups),
            daily_count=limiter.current_count(),
            daily_limit=config.DAILY_ACTION_LIMIT,
            skip_capture=skip_capture,
            status="running",
        )
        summary_result.update(
            {
                "target_likes": planned_count,
                "popup_actions": len(startup_popups),
                "daily_count": limiter.current_count(),
                "status": "running",
            }
        )

        attempt_idx = 1
        successful_likes = 0
        consecutive_errors = 0
        consecutive_unverified = 0
        total_errors = 0
        total_popup_actions = len(startup_popups)
        max_attempts = planned_count + max(3, min(8, planned_count))
        while successful_likes < planned_count and attempt_idx <= max_attempts:
            limiter.assert_available()
            try:
                should_check_popups = (
                    popup_check_every_n_actions > 0
                    and attempt_idx > 1
                    and attempt_idx % popup_check_every_n_actions == 0
                )
                handled_popups_before = (
                    handle_common_popups(device) if should_check_popups else []
                )
                profile_dir: Path | None = None
                text_lines: list[str] = []
                if not skip_capture:
                    profile_dir, page_context = save_current_customer_snapshot(
                        device,
                        appium,
                        output_dir=output_dir,
                        index=attempt_idx,
                        crop_bounds=crop_bounds,
                    )

                    text_lines = _profile_text_lines(page_context)
                    if _looks_like_no_swipe_target(page_context):
                        LOGGER.info("No swipe target detected on screen; stopping loop.")
                        with run_log.open("a", encoding="utf-8") as handle:
                            handle.write(
                                json.dumps(
                                    {
                                        "attempt": attempt_idx,
                                        "event": "stop_no_swipe_target",
                                        "profile_dir": str(profile_dir),
                                        "texts": text_lines,
                                        "successful_likes": successful_likes,
                                    },
                                    ensure_ascii=False,
                                )
                                + "\n"
                            )
                        break

                    LOGGER.info(
                        "Captured profile attempt %s; verified likes %s/%s at %s.",
                        attempt_idx,
                        successful_likes,
                        planned_count,
                        profile_dir,
                    )
                else:
                    LOGGER.info(
                        "Like-only mode: attempt %s; verified likes %s/%s.",
                        attempt_idx,
                        successful_likes,
                        planned_count,
                    )

                browse_operations = perform_random_profile_browse(
                    device,
                    config,
                    probability=view_profile_probability,
                    max_swipes=view_profile_max_swipes,
                    screen_size=screen_size,
                )
                if browse_operations:
                    LOGGER.info(
                        "Performed %s random profile-view swipe(s) before like.",
                        len(browse_operations),
                    )

                before_recovery_actions: list[str] = []
                before_snapshot = _capture_bumble_like_snapshot(device, label=f"before_like_{attempt_idx}")
                before_card = before_snapshot.get("card") or {}
                before_state = before_snapshot.get("state") or {}
                if (
                    not before_card.get("has_card")
                    and not before_state.get("is_no_target")
                    and not before_state.get("is_like_limit")
                ):
                    recovery = _recover_bumble_card_surface(device, start_tab=start_tab)
                    before_recovery_actions = list(recovery.get("actions") or [])
                    before_snapshot = recovery.get("snapshot") or before_snapshot
                    before_card = before_snapshot.get("card") or {}
                    before_state = before_snapshot.get("state") or {}
                if before_state.get("is_like_limit"):
                    LOGGER.info("Bumble like limit detected before swiping; stopping loop.")
                    with run_log.open("a", encoding="utf-8") as handle:
                        handle.write(
                            json.dumps(
                                {
                                    "attempt": attempt_idx,
                                    "event": "stop_like_limit",
                                    "before_card": before_card,
                                    "before_state": before_state,
                                    "successful_likes": successful_likes,
                                },
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
                    summary_result["status"] = "like_limit"
                    break
                if before_state.get("is_no_target") or not before_card.get("has_card"):
                    if not before_state.get("is_no_target"):
                        LOGGER.info(
                            "No visible card before swiping, but no hard stop marker was found; retrying recovery."
                        )
                        total_errors += 1
                        with run_log.open("a", encoding="utf-8") as handle:
                            handle.write(
                                json.dumps(
                                    {
                                        "attempt": attempt_idx,
                                        "event": "recoverable_no_card_before_swipe",
                                        "before_card": before_card,
                                        "before_state": before_state,
                                        "before_recovery_actions": before_recovery_actions,
                                        "successful_likes": successful_likes,
                                    },
                                    ensure_ascii=False,
                                )
                                + "\n"
                            )
                        _write_run_summary(
                            run_summary,
                            profile_id=profile_id,
                            output_dir=output_dir,
                            target_likes=planned_count,
                            successful_likes=successful_likes,
                            iteration_errors=total_errors,
                            popup_actions=total_popup_actions,
                            daily_count=limiter.current_count(),
                            daily_limit=config.DAILY_ACTION_LIMIT,
                            skip_capture=skip_capture,
                            status="recovering_no_card",
                        )
                        attempt_idx += 1
                        if total_errors >= max(6, planned_count // 4):
                            LOGGER.warning("Too many recoverable no-card states; stopping loop.")
                            summary_result["status"] = "no_card_stopped"
                            break
                        time.sleep(random.uniform(0.8, 1.6))
                        continue
                    LOGGER.info("No swipe target card detected before swiping; stopping loop.")
                    with run_log.open("a", encoding="utf-8") as handle:
                        handle.write(
                            json.dumps(
                                {
                                    "attempt": attempt_idx,
                                    "event": "stop_no_swipe_target",
                                    "before_card": before_card,
                                    "before_state": before_state,
                                    "successful_likes": successful_likes,
                                },
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
                    summary_result["status"] = "no_swipe_target"
                    break

                pre_swipe_wait = random.uniform(
                    config.PRE_LIKE_WAIT_MIN_SECONDS,
                    config.PRE_LIKE_WAIT_MAX_SECONDS,
                )
                LOGGER.info("Waiting %.2fs before right swipe.", pre_swipe_wait)
                time.sleep(pre_swipe_wait)

                start, end = _randomized_bumble_like_drag_coords(screen_size, strong=True)
                duration_ms = random.randint(880, 1320)
                points = human_bezier_swipe(
                    device,
                    start,
                    end,
                    total_duration_ms=duration_ms,
                    use_motion_events=False,
                    segmented_fallback=False,
                )
                if profile_dir is not None:
                    (profile_dir / "right_swipe_points.json").write_text(
                        json.dumps(points, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )

                verification = _verify_bumble_like_transition(
                    device,
                    before_snapshot,
                    attempts=config.BUMBLE_LIKE_VERIFY_ATTEMPTS,
                    wait_min_seconds=config.BUMBLE_LIKE_VERIFY_WAIT_MIN_SECONDS,
                    wait_max_seconds=config.BUMBLE_LIKE_VERIFY_WAIT_MAX_SECONDS,
                )
                fallback_points: list[tuple[int, int]] = []
                fallback_start: tuple[int, int] | None = None
                fallback_end: tuple[int, int] | None = None
                fallback_duration_ms: int | None = None
                if not verification.get("verified") and verification.get("status") in {
                    "card_unchanged",
                    "opened_profile_recovered",
                }:
                    fallback_start, fallback_end = _randomized_bumble_like_drag_coords(
                        screen_size,
                        strong=True,
                    )
                    fallback_duration_ms = random.randint(880, 1320)
                    LOGGER.warning(
                        "Right swipe did not change the visible card cleanly; retrying with one stronger long drag."
                    )
                    fallback_points = human_bezier_swipe(
                        device,
                        fallback_start,
                        fallback_end,
                        total_duration_ms=fallback_duration_ms,
                        use_motion_events=False,
                        segmented_fallback=False,
                    )
                    verification = _verify_bumble_like_transition(
                        device,
                        before_snapshot,
                        attempts=config.BUMBLE_LIKE_VERIFY_ATTEMPTS,
                        wait_min_seconds=config.BUMBLE_LIKE_VERIFY_WAIT_MIN_SECONDS,
                        wait_max_seconds=config.BUMBLE_LIKE_VERIFY_WAIT_MAX_SECONDS,
                    )

                verified_like = bool(verification.get("verified"))
                if verified_like:
                    successful_likes += 1
                    daily_count = limiter.consume()
                    consecutive_unverified = 0
                else:
                    daily_count = limiter.current_count()
                    consecutive_unverified += 1
                    total_errors += 1
                handled_popups_after = (
                    handle_common_popups(device, max_rounds=2)
                    if should_check_popups
                    else []
                )
                total_popup_actions += len(handled_popups_before) + len(handled_popups_after)
                post_like_recovery_actions: list[str] = []
                if verified_like and verification.get("status") == "matched":
                    recovery = _recover_bumble_card_surface(device, start_tab=start_tab)
                    post_like_recovery_actions = list(recovery.get("actions") or [])

                inter_wait = random.uniform(
                    config.RANDOM_WAIT_MIN_SECONDS,
                    config.RANDOM_WAIT_MAX_SECONDS,
                )
                stop_loop = bool(verification.get("stop_loop")) or verification.get("status") == "like_limit"
                limit_close_action: str | None = None
                if verification.get("status") == "like_limit":
                    limit_close_action = _click_visible_text_fast(
                        device,
                        ("Close", "Maybe later", "No thanks"),
                    )
                    if limit_close_action:
                        time.sleep(random.uniform(0.45, 0.9))
                if stop_loop:
                    status = str(verification.get("status") or "stopped")
                elif successful_likes >= planned_count:
                    status = "completed"
                elif consecutive_unverified >= 3:
                    status = "unverified_like_stopped"
                    stop_loop = True
                else:
                    status = "running"
                log_entry = {
                    "attempt": attempt_idx,
                    "event": "right_swipe_verified" if verified_like else "right_swipe_unverified",
                    "verification_status": verification.get("status"),
                    "verification_reason": verification.get("reason"),
                    "successful_likes": successful_likes,
                    "skip_capture": skip_capture,
                    "profile_dir": str(profile_dir) if profile_dir is not None else None,
                    "text_count": len(text_lines),
                    "popups_before": handled_popups_before,
                    "before_recovery_actions": before_recovery_actions,
                    "browse_operations": browse_operations,
                    "swipe_start": start,
                    "swipe_end": end,
                    "duration_ms": duration_ms,
                    "point_count": len(points),
                    "fallback_swipe_start": fallback_start,
                    "fallback_swipe_end": fallback_end,
                    "fallback_duration_ms": fallback_duration_ms,
                    "fallback_point_count": len(fallback_points),
                    "post_like_recovery_actions": post_like_recovery_actions,
                    "limit_close_action": limit_close_action,
                    "before_card": before_card,
                    "after_card": verification.get("after_card"),
                    "after_state": verification.get("after_state"),
                    "popups_after": handled_popups_after,
                    "daily_count": daily_count,
                    "daily_limit": config.DAILY_ACTION_LIMIT,
                    "next_wait_seconds": round(inter_wait, 3),
                }
                with run_log.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
                _write_run_summary(
                    run_summary,
                    profile_id=profile_id,
                    output_dir=output_dir,
                    target_likes=planned_count,
                    successful_likes=successful_likes,
                    iteration_errors=total_errors,
                    popup_actions=total_popup_actions,
                    daily_count=daily_count,
                    daily_limit=config.DAILY_ACTION_LIMIT,
                    skip_capture=skip_capture,
                    status=status,
                )
                summary_result.update(
                    {
                        "target_likes": planned_count,
                        "successful_likes": successful_likes,
                        "iteration_errors": total_errors,
                        "popup_actions": total_popup_actions,
                        "daily_count": daily_count,
                        "status": status,
                    }
                )
                if verified_like:
                    LOGGER.info(
                        "Verified like %s/%s on attempt %s; daily count %s/%s; next wait %.2fs.",
                        successful_likes,
                        planned_count,
                        attempt_idx,
                        daily_count,
                        config.DAILY_ACTION_LIMIT,
                        inter_wait,
                    )
                else:
                    LOGGER.warning(
                        "Right swipe attempt %s was not verified (%s); verified likes %s/%s.",
                        attempt_idx,
                        verification.get("status"),
                        successful_likes,
                        planned_count,
                    )
                consecutive_errors = 0
                attempt_idx += 1
                if stop_loop:
                    break
                if successful_likes < planned_count:
                    time.sleep(inter_wait)
            except DailyLimitExceeded:
                raise
            except (ADBCommandError, UIExtractionError, AutomationError) as exc:
                consecutive_errors += 1
                total_errors += 1
                LOGGER.warning(
                    "Swipe iteration %s failed (%s/%s): %s",
                    attempt_idx,
                    consecutive_errors,
                    3,
                    exc,
                )
                handled = handle_common_popups(device, max_rounds=4)
                total_popup_actions += len(handled)
                with run_log.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(
                            {
                                "attempt": attempt_idx,
                                "event": "iteration_error",
                                "error": str(exc),
                                "handled_popups": handled,
                                "retry": consecutive_errors,
                                "successful_likes": successful_likes,
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                if consecutive_errors >= 3:
                    raise AutomationError(
                        f"Stopping after {consecutive_errors} consecutive iteration errors."
                    ) from exc
                time.sleep(random.uniform(2.2, 4.5))
    finally:
        appium.quit()
        if stop_phone_after_run:
            try:
                GeelarkOpenAPIClient(config).stop_phone([profile_id])
                LOGGER.info("Stopped cloud phone %s after run.", profile_id)
            except Exception:
                LOGGER.debug("Failed to stop cloud phone %s.", profile_id, exc_info=True)
    return summary_result


def run_bumble_chat_capture(
    config: Config,
    *,
    profile_id: str,
    prepare_adb: bool = False,
    use_api_shell: bool = True,
    output_root: Path | None = None,
    max_chats: int = 1,
    chat_scrolls: int = 4,
    profile_scrolls: int = 4,
    stop_phone_after_run: bool = True,
    economy_mode: bool = False,
    phone_metadata: dict[str, Any] | None = None,
    save_screenshots: bool = False,
    list_only: bool = False,
    send_ai_replies: bool = False,
    capture_self_profile_for_ai: bool = False,
    target_location: str = "unknown",
    heat_threshold: int = 70,
    force_reply: bool = False,
    allow_global_self_profile_fallback: bool = True,
) -> dict[str, Any]:
    if max_chats <= 0:
        raise AutomationError("--max-chats must be greater than 0.")
    if chat_scrolls < 0 or profile_scrolls < 0:
        raise AutomationError("--chat-scrolls and --profile-scrolls must be >= 0.")

    if economy_mode:
        config = replace(
            config,
            BUMBLE_OPEN_WAIT_MIN_SECONDS=1.2,
            BUMBLE_OPEN_WAIT_MAX_SECONDS=2.0,
            BUMBLE_TAB_WAIT_MIN_SECONDS=0.75,
            BUMBLE_TAB_WAIT_MAX_SECONDS=1.35,
            SELF_PROFILE_SCROLLS=3,
        )

    config = replace(
        config,
        GEELARK_PROFILE_ID=profile_id,
        AUTO_START_PHONE=prepare_adb,
        AUTO_ENABLE_ADB=prepare_adb,
        LOOP_FOREVER=False,
    )
    device, appium = _build_device_for_profile(config, use_api_shell=use_api_shell)
    output_dir = (
        output_root
        if output_root is not None
        else Path.cwd() / "diagnostics" / profile_id / "chat_capture" / _timestamp_for_path()
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / "chat_capture_summary.json"
    monitor_path = output_dir / "reply_monitor.jsonl"

    summary: dict[str, Any] = {
        "profile_id": profile_id,
        "phone": phone_metadata or {},
        "output_dir": str(output_dir),
        "max_chats": max_chats,
        "chat_scrolls": chat_scrolls,
        "profile_scrolls": profile_scrolls,
        "save_screenshots": save_screenshots,
        "list_only": list_only,
        "send_ai_replies": send_ai_replies,
        "capture_self_profile_for_ai": capture_self_profile_for_ai,
        "target_location": target_location,
        "heat_threshold": heat_threshold,
        "allow_global_self_profile_fallback": allow_global_self_profile_fallback,
        "status": "not_started",
        "chat_list_text_count": 0,
        "opened_chats": 0,
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    _append_reply_monitor_event(
        monitor_path,
        {
            "event": "phone_run_started",
            "profile_id": profile_id,
            "phone": phone_metadata or {},
            "send_ai_replies": send_ai_replies,
            "max_chats": max_chats,
            "chat_scrolls": chat_scrolls,
            "profile_scrolls": profile_scrolls,
            "output_dir": str(output_dir),
        },
    )

    try:
        device.ensure_connected()
        _append_reply_monitor_event(
            monitor_path,
            {"event": "device_connected", "profile_id": profile_id},
        )
        if send_ai_replies and config.AUTO_INSTALL_ADB_KEYBOARD:
            try:
                _ensure_adb_keyboard_enabled(device, config)
                summary["unicode_input"] = {
                    "status": "enabled",
                    "method": "ADBKeyboard",
                    "ime": config.ADB_KEYBOARD_IME,
                }
                _append_reply_monitor_event(
                    monitor_path,
                    {
                        "event": "unicode_input_enabled",
                        "profile_id": profile_id,
                        "method": "ADBKeyboard",
                        "ime": config.ADB_KEYBOARD_IME,
                    },
                )
            except Exception as exc:
                summary["unicode_input"] = {
                    "status": "failed",
                    "method": "ADBKeyboard",
                    "error": str(exc),
                }
                LOGGER.warning("Could not enable ADBKeyboard Unicode input: %s", exc)
                _append_reply_monitor_event(
                    monitor_path,
                    {
                        "event": "unicode_input_enable_failed",
                        "profile_id": profile_id,
                        "method": "ADBKeyboard",
                        "error": str(exc),
                    },
                )
            summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        appium.connect()
        self_profile_text = ""
        self_profile_source = ""
        self_profile_needs_capture = False
        if send_ai_replies:
            self_profile_text, self_profile_source = _find_self_profile_text_for_phone(profile_id)
            if self_profile_text:
                LOGGER.info("Using cached self profile for AI context: %s", self_profile_source)
            if not self_profile_text and allow_global_self_profile_fallback:
                self_profile_text, self_profile_source = _find_latest_self_profile_text()
            if not self_profile_text and capture_self_profile_for_ai:
                self_profile_needs_capture = True
            summary["self_profile_source"] = self_profile_source
            summary["self_profile_text_count"] = len(
                [line for line in self_profile_text.splitlines() if line.strip()]
            )
            summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

        open_bumble_tab(device, "chats")
        _append_reply_monitor_event(
            monitor_path,
            {"event": "chats_tab_opened", "profile_id": profile_id},
        )
        dismissed = _click_visible_text_fast(
            device,
            ("Close", "Not now", "No thanks", "Maybe later", "Got it"),
        )
        if dismissed:
            LOGGER.info("Dismissed chat popup/action by text: %s.", dismissed)
            time.sleep(random.uniform(0.55, 1.0))
            dismissed_again = _click_visible_text_fast(
                device,
                ("Close", "Not now", "No thanks", "Maybe later", "Got it"),
            )
            if dismissed_again:
                LOGGER.info("Dismissed second chat popup/action by text: %s.", dismissed_again)
                time.sleep(random.uniform(0.55, 1.0))

        chat_list_dir = output_dir / "chat_list"
        chat_list_dir.mkdir(parents=True, exist_ok=True)
        chat_list_snapshot = _collect_visible_text_snapshot(
            device,
            label="chat_list_initial",
            output_dir=chat_list_dir,
        )
        _save_screen_if_requested(
            device,
            chat_list_dir / "chat_list_initial.png",
            save_screenshots,
        )
        if "com.bumble.app/" not in str(chat_list_snapshot.get("activity") or ""):
            LOGGER.warning(
                "Chat list capture is not in Bumble (activity=%s); reopening once.",
                chat_list_snapshot.get("activity"),
            )
            _force_relaunch_bumble(device, reason="chat_list_not_in_bumble")
            open_bumble_tab(device, "chats")
            chat_list_snapshot = _collect_visible_text_snapshot(
                device,
                label="chat_list_non_bumble_retry",
                output_dir=chat_list_dir,
            )
            _save_screen_if_requested(
                device,
                chat_list_dir / "chat_list_non_bumble_retry.png",
                save_screenshots,
            )
        if _looks_like_bumble_chat_thread(
            chat_list_snapshot["texts"],
            str(chat_list_snapshot.get("activity") or ""),
        ):
            LOGGER.info(
                "Chats tab opened inside an existing conversation; backing out to the chat list."
            )
            for _ in range(2):
                device.shell("input", "keyevent", "BACK", timeout=6, check=False)
                time.sleep(random.uniform(0.35, 0.7))
                current_values = _extract_visible_text_values_fast(device)
                if _looks_like_bumble_chat_list(current_values):
                    break
            open_bumble_tab(device, "chats")
            chat_list_snapshot = _collect_visible_text_snapshot(
                device,
                label="chat_list_thread_retry",
                output_dir=chat_list_dir,
            )
            _save_screen_if_requested(
                device,
                chat_list_dir / "chat_list_thread_retry.png",
                save_screenshots,
            )
        if _looks_like_bumble_non_chat_surface(
            chat_list_snapshot["texts"],
            str(chat_list_snapshot.get("activity") or ""),
        ):
            LOGGER.warning(
                "Chat list capture is on a non-chat Bumble surface (activity=%s); reopening once.",
                chat_list_snapshot.get("activity"),
            )
            _force_relaunch_bumble(device, reason="chat_list_non_chat_surface")
            open_bumble_tab(device, "chats")
            chat_list_snapshot = _collect_visible_text_snapshot(
                device,
                label="chat_list_non_chat_retry",
                output_dir=chat_list_dir,
            )
            _save_screen_if_requested(
                device,
                chat_list_dir / "chat_list_non_chat_retry.png",
                save_screenshots,
            )
        if not chat_list_snapshot["texts"]:
            LOGGER.warning("Chat list text was empty; reopening Chats tab once.")
            open_bumble_tab(device, "chats")
            chat_list_snapshot = _collect_visible_text_snapshot(
                device,
                label="chat_list_retry",
                output_dir=chat_list_dir,
            )
            _save_screen_if_requested(
                device,
                chat_list_dir / "chat_list_retry.png",
                save_screenshots,
            )
        if (
            not _snapshot_looks_like_bumble_chat_list(chat_list_snapshot)
            and not _looks_like_empty_bumble_chat_list(chat_list_snapshot["texts"])
        ):
            LOGGER.warning(
                "Chats tab capture does not look like the chat list; reopening Chats tab once."
            )
            open_bumble_tab(device, "chats")
            chat_list_snapshot = _collect_visible_text_snapshot(
                device,
                label="chat_list_surface_retry",
                output_dir=chat_list_dir,
            )
            _save_screen_if_requested(
                device,
                chat_list_dir / "chat_list_surface_retry.png",
                save_screenshots,
            )
            if (
                not _snapshot_looks_like_bumble_chat_list(chat_list_snapshot)
                and not _looks_like_empty_bumble_chat_list(chat_list_snapshot["texts"])
            ):
                LOGGER.warning(
                    "Chats tab still does not look like the chat list; force-relaunching Bumble once."
                )
                _force_relaunch_bumble(device, reason="chat_list_surface_retry_failed")
                open_bumble_tab(device, "chats")
                chat_list_snapshot = _collect_visible_text_snapshot(
                    device,
                    label="chat_list_surface_force_retry",
                    output_dir=chat_list_dir,
                )
                _save_screen_if_requested(
                    device,
                    chat_list_dir / "chat_list_surface_force_retry.png",
                    save_screenshots,
                )
        if (
            not _snapshot_looks_like_bumble_chat_list(chat_list_snapshot)
            and not _looks_like_empty_bumble_chat_list(chat_list_snapshot["texts"])
        ):
            summary.update(
                {
                    "status": "chat_list_unavailable",
                    "error": "Chats tab did not expose a recognizable chat list after retries.",
                    "chat_list_text_count": len(chat_list_snapshot["texts"]),
                    "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                }
            )
            summary_path.write_text(
                json.dumps(summary, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            _append_reply_monitor_event(
                monitor_path,
                {
                    "event": "phone_run_completed",
                    "profile_id": profile_id,
                    "status": "chat_list_unavailable",
                    "activity": chat_list_snapshot.get("activity"),
                    "text_count": len(chat_list_snapshot["texts"]),
                    "chat_list_dir": str(chat_list_dir),
                },
            )
            return summary

        screen_size = device.get_screen_size()
        candidates = _find_chat_list_candidates(
            chat_list_snapshot["nodes"],
            screen_size=screen_size,
        )
        _append_reply_monitor_event(
            monitor_path,
            {
                "event": "chat_list_captured",
                "profile_id": profile_id,
                "activity": chat_list_snapshot.get("activity"),
                "text_count": len(chat_list_snapshot["texts"]),
                "candidate_count": len(candidates),
                "candidate_labels": [
                    str(candidate.get("label") or "") for candidate in candidates[:8]
                ],
                "chat_list_dir": str(chat_list_dir),
            },
        )
        if not candidates:
            LOGGER.info("No readable chat rows found; scrolling chat list once before fallback.")
            human_bezier_swipe(
                device,
                _relative_point(screen_size, 0.50, 0.78),
                _relative_point(screen_size, 0.50, 0.35),
                min_intermediate_points=15,
                total_duration_ms=random.randint(580, 880),
                use_motion_events=_prefer_motion_events(device),
            )
            time.sleep(random.uniform(0.55, 1.0))
            chat_list_snapshot = _collect_visible_text_snapshot(
                device,
                label="chat_list_after_scroll_1",
                output_dir=chat_list_dir,
            )
            candidates = _find_chat_list_candidates(
                chat_list_snapshot["nodes"],
                screen_size=screen_size,
            )
            _append_reply_monitor_event(
                monitor_path,
                {
                    "event": "chat_list_after_scroll",
                    "profile_id": profile_id,
                    "activity": chat_list_snapshot.get("activity"),
                    "text_count": len(chat_list_snapshot["texts"]),
                    "candidate_count": len(candidates),
                    "candidate_labels": [
                        str(candidate.get("label") or "") for candidate in candidates[:8]
                    ],
                    "chat_list_dir": str(chat_list_dir),
                },
            )
        if (
            send_ai_replies
            and not list_only
            and candidates
            and not any(_candidate_row_needs_reply(candidate) for candidate in candidates)
            and not any(_candidate_is_recent_chat_row(candidate) for candidate in candidates)
            and _looks_like_bumble_chat_list(chat_list_snapshot["texts"])
            and not _looks_like_empty_bumble_chat_list(chat_list_snapshot["texts"])
        ):
            for scan_idx in range(2):
                LOGGER.info(
                    "Only top-match/non-chat rows were visible; scrolling chat list to find recent conversations (%s/2).",
                    scan_idx + 1,
                )
                human_bezier_swipe(
                    device,
                    _relative_point(screen_size, 0.50, 0.78),
                    _relative_point(screen_size, 0.50, 0.35),
                    min_intermediate_points=15,
                    total_duration_ms=random.randint(580, 880),
                    use_motion_events=_prefer_motion_events(device),
                )
                time.sleep(random.uniform(0.55, 1.0))
                chat_list_snapshot = _collect_visible_text_snapshot(
                    device,
                    label=f"chat_list_recent_scan_{scan_idx + 1}",
                    output_dir=chat_list_dir,
                )
                if not _snapshot_looks_like_bumble_chat_list(chat_list_snapshot):
                    LOGGER.info(
                        "Scroll landed in a non-chat-list surface (activity=%s); backing out to the chat list.",
                        chat_list_snapshot.get("activity"),
                    )
                    for _ in range(2):
                        device.shell("input", "keyevent", "BACK", timeout=6, check=False)
                        time.sleep(random.uniform(0.4, 0.8))
                    open_bumble_tab(device, "chats")
                    time.sleep(random.uniform(0.8, 1.4))
                    chat_list_snapshot = _collect_visible_text_snapshot(
                        device,
                        label=f"chat_list_recent_scan_{scan_idx + 1}_recovered",
                        output_dir=chat_list_dir,
                    )
                scanned_candidates = _find_chat_list_candidates(
                    chat_list_snapshot["nodes"],
                    screen_size=screen_size,
                )
                _append_reply_monitor_event(
                    monitor_path,
                    {
                        "event": "chat_list_recent_scan",
                        "profile_id": profile_id,
                        "activity": chat_list_snapshot.get("activity"),
                        "text_count": len(chat_list_snapshot["texts"]),
                        "candidate_count": len(scanned_candidates),
                        "candidate_labels": [
                            str(candidate.get("label") or "")
                            for candidate in scanned_candidates[:8]
                        ],
                        "chat_list_dir": str(chat_list_dir),
                    },
                )
                if scanned_candidates:
                    candidates = scanned_candidates
                if any(_candidate_row_needs_reply(candidate) for candidate in candidates) or any(
                    _candidate_is_recent_chat_row(candidate) for candidate in candidates
                ):
                    break
        if (
            not candidates
            and not list_only
            and _looks_like_bumble_chat_list(chat_list_snapshot["texts"])
            and not _looks_like_empty_bumble_chat_list(chat_list_snapshot["texts"])
        ):
            candidates = _blind_chat_list_fallback_candidates(
                screen_size=screen_size,
                max_rows=max(8, min(12, max_chats * 4)),
            )
            LOGGER.info(
                "UI tree exposed no chat rows; trying %s blind chat-list row tap(s).",
                len(candidates),
            )
            _append_reply_monitor_event(
                monitor_path,
                {
                    "event": "chat_list_blind_fallback_enabled",
                    "profile_id": profile_id,
                    "activity": chat_list_snapshot.get("activity"),
                    "text_count": len(chat_list_snapshot["texts"]),
                    "candidate_count": len(candidates),
                    "candidate_labels": [
                        str(candidate.get("label") or "") for candidate in candidates
                    ],
                    "reason": "ui_tree_header_only",
                },
            )
        (chat_list_dir / "candidates.json").write_text(
            json.dumps(candidates, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        reply_audit = [
            _build_reply_audit_entry(index, candidate)
            for index, candidate in enumerate(candidates, start=1)
        ]
        reply_audit_by_key = {
            str(item.get("key") or ""): item
            for item in reply_audit
            if str(item.get("key") or "")
        }
        reply_audit_paths = _write_reply_audit_files(output_dir, reply_audit)
        unreplied_audit = _reply_audit_needs_attention(reply_audit)
        summary.update(
            {
                "status": "chat_list_captured",
                "chat_list_text_count": len(chat_list_snapshot["texts"]),
                "chat_candidates": candidates[:12],
                "reply_audit_file": reply_audit_paths["json"],
                "reply_audit_text_file": reply_audit_paths["txt"],
                "unreplied_audit_count": len(unreplied_audit),
                "unreplied_audit": unreplied_audit[:20],
            }
        )
        summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if not candidates and _looks_like_empty_bumble_chat_list(chat_list_snapshot["texts"]):
            summary.update(
                {
                    "status": "no_chat_candidates",
                    "opened_chats": 0,
                    "chats": [],
                    "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                }
            )
            summary_path.write_text(
                json.dumps(summary, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            _append_reply_monitor_event(
                monitor_path,
                {
                    "event": "phone_run_completed",
                    "profile_id": profile_id,
                    "status": "no_chat_candidates",
                    "opened_chats": 0,
                    "reason": "empty_bumble_chat_list",
                    "reply_audit_file": reply_audit_paths["json"],
                },
            )
            return summary
        if not candidates:
            summary.update(
                {
                    "status": "no_chat_candidates",
                    "opened_chats": 0,
                    "chats": [],
                    "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                }
            )
            summary_path.write_text(
                json.dumps(summary, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            _append_reply_monitor_event(
                monitor_path,
                {
                    "event": "phone_run_completed",
                    "profile_id": profile_id,
                    "status": "no_chat_candidates",
                    "opened_chats": 0,
                    "reason": "no_clickable_candidates",
                    "reply_audit_file": reply_audit_paths["json"],
                },
            )
            return summary
        if list_only:
            summary.update(
                {
                    "status": "chat_list_only",
                    "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                }
            )
            summary_path.write_text(
                json.dumps(summary, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            _append_reply_monitor_event(
                monitor_path,
                {
                    "event": "phone_run_completed",
                    "profile_id": profile_id,
                    "status": "chat_list_only",
                    "opened_chats": 0,
                    "reply_audit_file": reply_audit_paths["json"],
                },
            )
            return summary

        if send_ai_replies and self_profile_needs_capture:
            try:
                self_profile_summary = save_bumble_self_profile(
                    device,
                    appium,
                    output_dir=output_dir,
                    phone_metadata=phone_metadata,
                )
                self_profile_text = "\n".join(
                    str(value)
                    for value in self_profile_summary.get("texts", [])
                    if str(value).strip()
                )
                if self_profile_text:
                    self_profile_source = str(output_dir / "self_profile" / "texts.txt")
            except Exception as exc:
                LOGGER.warning("Deferred self profile capture for AI failed: %s", exc)
            if not self_profile_text and allow_global_self_profile_fallback:
                self_profile_text, self_profile_source = _find_latest_self_profile_text()
            summary["self_profile_source"] = self_profile_source
            summary["self_profile_text_count"] = len(
                [line for line in self_profile_text.splitlines() if line.strip()]
            )
            summary_path.write_text(
                json.dumps(summary, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            open_bumble_tab(device, "chats")
            chat_list_snapshot = _collect_visible_text_snapshot(
                device,
                label="chat_list_after_self_profile",
                output_dir=chat_list_dir,
            )
            _save_screen_if_requested(
                device,
                chat_list_dir / "chat_list_after_self_profile.png",
                save_screenshots,
            )
            if not _snapshot_looks_like_bumble_chat_list(chat_list_snapshot):
                LOGGER.warning(
                    "Post-self-profile capture was not the chat list; reopening Chats tab once."
                )
                open_bumble_tab(device, "chats")
                chat_list_snapshot = _collect_visible_text_snapshot(
                    device,
                    label="chat_list_after_self_profile_surface_retry",
                    output_dir=chat_list_dir,
                )
                _save_screen_if_requested(
                    device,
                    chat_list_dir / "chat_list_after_self_profile_surface_retry.png",
                    save_screenshots,
                )
                if not _snapshot_looks_like_bumble_chat_list(chat_list_snapshot):
                    LOGGER.warning(
                        "Post-self-profile Chats retry still was not the chat list; force-relaunching Bumble once."
                    )
                    _force_relaunch_bumble(
                        device,
                        reason="post_self_profile_chat_list_surface_retry_failed",
                    )
                    open_bumble_tab(device, "chats")
                    chat_list_snapshot = _collect_visible_text_snapshot(
                        device,
                        label="chat_list_after_self_profile_force_retry",
                        output_dir=chat_list_dir,
                    )
                    _save_screen_if_requested(
                        device,
                        chat_list_dir / "chat_list_after_self_profile_force_retry.png",
                        save_screenshots,
                    )
            if _looks_like_bumble_chat_thread(
                chat_list_snapshot["texts"],
                str(chat_list_snapshot.get("activity") or ""),
            ):
                for _ in range(2):
                    device.shell("input", "keyevent", "BACK", timeout=6, check=False)
                    time.sleep(random.uniform(0.35, 0.7))
                    current_values = _extract_visible_text_values_fast(device)
                    if _looks_like_bumble_chat_list(current_values):
                        break
                open_bumble_tab(device, "chats")
                chat_list_snapshot = _collect_visible_text_snapshot(
                    device,
                    label="chat_list_after_self_profile_thread_retry",
                    output_dir=chat_list_dir,
                )
                _save_screen_if_requested(
                    device,
                    chat_list_dir / "chat_list_after_self_profile_thread_retry.png",
                    save_screenshots,
                )
            if (
                not _snapshot_looks_like_bumble_chat_list(chat_list_snapshot)
                and not _looks_like_empty_bumble_chat_list(chat_list_snapshot["texts"])
            ):
                summary.update(
                    {
                        "status": "chat_list_unavailable_after_self_profile",
                        "error": (
                            "Chats tab did not expose a recognizable chat list after self-profile capture."
                        ),
                        "chat_list_text_count": len(chat_list_snapshot["texts"]),
                        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    }
                )
                summary_path.write_text(
                    json.dumps(summary, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                _append_reply_monitor_event(
                    monitor_path,
                    {
                        "event": "phone_run_completed",
                        "profile_id": profile_id,
                        "status": "chat_list_unavailable_after_self_profile",
                        "activity": chat_list_snapshot.get("activity"),
                        "text_count": len(chat_list_snapshot["texts"]),
                        "chat_list_dir": str(chat_list_dir),
                    },
                )
                return summary
            candidates = _find_chat_list_candidates(
                chat_list_snapshot["nodes"],
                screen_size=screen_size,
            )
            (chat_list_dir / "candidates_after_self_profile.json").write_text(
                json.dumps(candidates, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            reply_audit = [
                _build_reply_audit_entry(index, candidate)
                for index, candidate in enumerate(candidates, start=1)
            ]
            reply_audit_by_key = {
                str(item.get("key") or ""): item
                for item in reply_audit
                if str(item.get("key") or "")
            }
            reply_audit_paths = _write_reply_audit_files(output_dir, reply_audit)
            unreplied_audit = _reply_audit_needs_attention(reply_audit)
            summary.update(
                {
                    "status": "chat_list_recaptured_after_self_profile",
                    "chat_list_text_count": len(chat_list_snapshot["texts"]),
                    "chat_candidates": candidates[:12],
                    "reply_audit_file": reply_audit_paths["json"],
                    "reply_audit_text_file": reply_audit_paths["txt"],
                    "unreplied_audit_count": len(unreplied_audit),
                    "unreplied_audit": unreplied_audit[:20],
                }
            )
            summary_path.write_text(
                json.dumps(summary, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            if not candidates and _looks_like_empty_bumble_chat_list(chat_list_snapshot["texts"]):
                summary.update(
                    {
                        "status": "no_chat_candidates",
                        "opened_chats": 0,
                        "chats": [],
                        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    }
                )
                summary_path.write_text(
                    json.dumps(summary, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                _append_reply_monitor_event(
                    monitor_path,
                    {
                        "event": "phone_run_completed",
                        "profile_id": profile_id,
                        "status": "no_chat_candidates",
                        "opened_chats": 0,
                        "reason": "empty_after_self_profile_capture",
                    },
                )
                return summary
            if not candidates:
                summary.update(
                    {
                        "status": "no_chat_candidates",
                        "opened_chats": 0,
                        "chats": [],
                        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    }
                )
                summary_path.write_text(
                    json.dumps(summary, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                _append_reply_monitor_event(
                    monitor_path,
                    {
                        "event": "phone_run_completed",
                        "profile_id": profile_id,
                        "status": "no_chat_candidates",
                        "opened_chats": 0,
                        "reason": "no_candidates_after_self_profile_capture",
                    },
                )
                return summary

        actionable_candidates = list(candidates)
        if send_ai_replies and not force_reply:
            needs_reply_candidates = [
                candidate for candidate in candidates if _candidate_row_needs_reply(candidate)
            ]
            needs_reply_keys = {
                _reply_candidate_key(
                    str(candidate.get("label") or ""),
                    candidate.get("click_center") or candidate.get("node_center"),
                )
                for candidate in needs_reply_candidates
            }
            recent_chat_candidates = [
                candidate
                for candidate in candidates
                if _candidate_is_recent_chat_row(candidate)
                and _reply_candidate_key(
                    str(candidate.get("label") or ""),
                    candidate.get("click_center") or candidate.get("node_center"),
                )
                not in needs_reply_keys
            ]
            actionable_candidates = needs_reply_candidates + recent_chat_candidates
        click_attempts: list[dict[str, Any]] = [
            {
                "source": "candidate",
                "label": candidate["label"],
                "point": tuple(candidate["click_center"]),
                "candidate_key": _reply_candidate_key(
                    str(candidate.get("label") or ""),
                    candidate.get("click_center") or candidate.get("node_center"),
                ),
                "needs_reply_from_list": _candidate_row_needs_reply(candidate),
                "row_context": list(candidate.get("row_context") or []),
            }
            for candidate in actionable_candidates[: max(1, max_chats * 3)]
        ]
        fallback_points: tuple[tuple[int, int], ...] = ()
        seen_attempt_points = {tuple(attempt["point"]) for attempt in click_attempts}
        for idx, point in enumerate(fallback_points, start=1):
            if tuple(point) not in seen_attempt_points:
                click_attempts.append(
                    {"source": "fallback", "label": f"fallback_{idx}", "point": point}
                )
                seen_attempt_points.add(tuple(point))

        def mark_attempt_audit(
            attempt: dict[str, Any],
            *,
            status: str,
            reason: str = "",
            **extra: Any,
        ) -> None:
            audit_key = str(attempt.get("candidate_key") or "")
            if not audit_key:
                audit_key = _reply_candidate_key(
                    str(attempt.get("label") or ""),
                    attempt.get("point"),
                )
            audit_entry = reply_audit_by_key.get(audit_key)
            if audit_entry is None:
                return
            audit_entry.update(
                {
                    "status": status,
                    "reason": reason,
                    "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    **extra,
                }
            )
            _write_reply_audit_files(output_dir, reply_audit)

        opened_results: list[dict[str, Any]] = []
        replied_chat_titles_this_run: set[str] = set()
        for attempt_idx, attempt in enumerate(click_attempts, start=1):
            if len(opened_results) >= max_chats:
                break
            chat_dir = output_dir / f"chat_{len(opened_results) + 1:02d}"
            chat_dir.mkdir(parents=True, exist_ok=True)
            LOGGER.info(
                "Opening chat candidate %s/%s: %s at %s.",
                attempt_idx,
                len(click_attempts),
                attempt["label"],
                attempt["point"],
            )
            if attempt_idx > 1 and str(attempt.get("source") or "") == "candidate":
                current_activity = device.get_current_activity()
                if "com.bumble.app/" not in current_activity:
                    LOGGER.info(
                        "Returning to Bumble chats before next candidate; current activity=%s.",
                        current_activity,
                    )
                    _force_relaunch_bumble(device, reason="candidate_retry_not_on_chat_list")
                open_bumble_tab(device, "chats")
                fresh_chat_list_dir = output_dir / "chat_list_rescans"
                fresh_chat_list_dir.mkdir(parents=True, exist_ok=True)
                fresh_snapshot = _collect_visible_text_snapshot(
                    device,
                    label=f"before_attempt_{attempt_idx}",
                    output_dir=fresh_chat_list_dir,
                )
                if not _snapshot_looks_like_bumble_chat_list(fresh_snapshot):
                    LOGGER.info(
                        "Candidate rescan was not on the chat list; reopening Chats tab before matching %s.",
                        attempt.get("label"),
                    )
                    open_bumble_tab(device, "chats")
                    fresh_snapshot = _collect_visible_text_snapshot(
                        device,
                        label=f"before_attempt_{attempt_idx}_chats_retry",
                        output_dir=fresh_chat_list_dir,
                    )
                    if not _snapshot_looks_like_bumble_chat_list(fresh_snapshot):
                        LOGGER.info(
                            "Candidate rescan still was not on the chat list; force-relaunching before matching %s.",
                            attempt.get("label"),
                        )
                        _force_relaunch_bumble(
                            device,
                            reason="candidate_rescan_chat_list_retry_failed",
                        )
                        open_bumble_tab(device, "chats")
                        fresh_snapshot = _collect_visible_text_snapshot(
                            device,
                            label=f"before_attempt_{attempt_idx}_force_retry",
                            output_dir=fresh_chat_list_dir,
                        )
                fresh_candidates = _find_chat_list_candidates(
                    fresh_snapshot["nodes"],
                    screen_size=screen_size,
                )
                wanted_label = str(attempt.get("label") or "").strip()
                attempt_requires_list_need = bool(attempt.get("needs_reply_from_list"))
                fresh_match = next(
                    (
                        candidate
                        for candidate in fresh_candidates
                        if _chat_candidate_matches_label(candidate, wanted_label)
                        and (
                            _candidate_row_needs_reply(candidate)
                            if attempt_requires_list_need
                            else _candidate_is_recent_chat_row(candidate)
                        )
                    ),
                    None,
                )
                if fresh_match is None:
                    LOGGER.info(
                        "Skipping stale candidate %s; it no longer matched after list rescan.",
                        attempt.get("label"),
                    )
                    mark_attempt_audit(
                        attempt,
                        status="skipped_not_actionable_after_rescan",
                        reason=(
                            "chat row no longer showed your move/unread before opening"
                            if attempt_requires_list_need
                            else "recent chat row was no longer visible before opening"
                        ),
                        chat_list_rescan_dir=str(fresh_chat_list_dir),
                    )
                    continue
                attempt["point"] = tuple(fresh_match["click_center"])
                attempt["row_context"] = list(fresh_match.get("row_context") or [])
                attempt["needs_reply_from_list"] = True
            _append_reply_monitor_event(
                monitor_path,
                {
                    "event": "chat_open_attempt",
                    "profile_id": profile_id,
                    "attempt_idx": attempt_idx,
                    "attempt": attempt,
                    "chat_dir": str(chat_dir),
                },
            )
            human_gaussian_click(
                device,
                int(attempt["point"][0]),
                int(attempt["point"][1]),
                sigma_px=9.0,
                max_offset_px=26,
            )
            time.sleep(random.uniform(1.1, 1.8))
            first_snapshot = _collect_visible_text_snapshot(
                device,
                label="conversation_initial",
                output_dir=chat_dir,
            )
            _save_screen_if_requested(
                device,
                chat_dir / "conversation_initial.png",
                save_screenshots,
            )
            if _looks_like_bumble_non_chat_surface(
                first_snapshot["texts"],
                first_snapshot["activity"],
            ):
                LOGGER.info("Candidate opened a non-chat Bumble surface; returning to chat list.")
                mark_attempt_audit(
                    attempt,
                    status="skipped_opened_non_chat_surface",
                    reason="candidate tap opened a Bumble surface that is not a chat thread",
                    activity=first_snapshot.get("activity"),
                    chat_dir=str(chat_dir),
                )
                _append_reply_monitor_event(
                    monitor_path,
                    {
                        "event": "chat_open_skipped",
                        "profile_id": profile_id,
                        "attempt_idx": attempt_idx,
                        "reason": "opened_non_chat_surface",
                        "activity": first_snapshot.get("activity"),
                        "chat_dir": str(chat_dir),
                    },
                )
                for _ in range(2):
                    device.shell("input", "keyevent", "BACK", timeout=6, check=False)
                    time.sleep(random.uniform(0.35, 0.7))
                continue

            opened = _looks_like_bumble_chat_thread(
                first_snapshot["texts"],
                first_snapshot["activity"],
            )
            if not opened:
                LOGGER.info("Candidate did not open a chat; returning to chat list.")
                mark_attempt_audit(
                    attempt,
                    status="failed_not_a_chat_thread",
                    reason="candidate tap did not open a readable chat thread",
                    activity=first_snapshot.get("activity"),
                    text_count=len(first_snapshot.get("texts", [])),
                    chat_dir=str(chat_dir),
                )
                _append_reply_monitor_event(
                    monitor_path,
                    {
                        "event": "chat_open_skipped",
                        "profile_id": profile_id,
                        "attempt_idx": attempt_idx,
                        "reason": "not_a_chat_thread",
                        "activity": first_snapshot.get("activity"),
                        "text_count": len(first_snapshot.get("texts", [])),
                        "chat_dir": str(chat_dir),
                    },
                )
                if "appmainactivity" not in str(first_snapshot["activity"]).lower():
                    device.shell("input", "keyevent", "BACK", timeout=6, check=False)
                    time.sleep(random.uniform(0.5, 0.9))
                continue

            conversation_snapshots = [first_snapshot]
            for scroll_idx in range(chat_scrolls):
                LOGGER.info("Capturing chat history scroll %s/%s.", scroll_idx + 1, chat_scrolls)
                start = _relative_point(screen_size, 0.50, 0.34)
                end = _relative_point(screen_size, 0.50, 0.78)
                human_bezier_swipe(
                    device,
                    start,
                    end,
                    min_intermediate_points=15,
                    total_duration_ms=random.randint(620, 980),
                    use_motion_events=_prefer_motion_events(device),
                )
                time.sleep(random.uniform(0.55, 1.05))
                scroll_label = f"conversation_scroll_{scroll_idx + 1}"
                conversation_snapshots.append(
                    _collect_visible_text_snapshot(
                        device,
                        label=scroll_label,
                        output_dir=chat_dir,
                    )
                )
                _save_screen_if_requested(
                    device,
                    chat_dir / f"{scroll_label}.png",
                    save_screenshots,
                )

            conversation_texts = _dedupe_strings(
                text
                for snapshot in conversation_snapshots
                for text in snapshot["texts"]
            )
            _write_text_lines(chat_dir / "conversation_texts.txt", conversation_texts)
            chat_title = _chat_title_from_snapshot(first_snapshot)
            if _chat_title_is_generic(chat_title):
                fallback_chat_title = _chat_title_from_candidate_label(
                    str(attempt.get("label") or "")
                )
                if fallback_chat_title:
                    chat_title = fallback_chat_title
            chat_title_run_key = chat_title.strip().lower()
            duplicate_replied_chat_this_run = (
                bool(chat_title_run_key)
                and chat_title_run_key in replied_chat_titles_this_run
                and not force_reply
            )
            conversation_events = _extract_conversation_events(
                first_snapshot,
                screen_size=screen_size,
            )
            (chat_dir / "conversation_events.json").write_text(
                json.dumps(conversation_events, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (chat_dir / "conversation_events.txt").write_text(
                _conversation_events_to_text(conversation_events),
                encoding="utf-8",
            )
            conversation_message_texts = _filter_chat_message_texts(
                conversation_texts,
                chat_title=chat_title,
            )
            _write_text_lines(
                chat_dir / "conversation_messages.txt",
                conversation_message_texts,
            )
            last_visible_event_for_monitor = _last_message_event(conversation_events)
            _append_reply_monitor_event(
                monitor_path,
                {
                    "event": "conversation_captured",
                    "profile_id": profile_id,
                    "chat_title": chat_title,
                    "chat_dir": str(chat_dir),
                    "conversation_text_count": len(conversation_texts),
                    "message_text_count": len(conversation_message_texts),
                    "event_count": len(conversation_events),
                    "last_message_sender": (
                        last_visible_event_for_monitor or {}
                    ).get("sender"),
                    "last_message_text": (
                        last_visible_event_for_monitor or {}
                    ).get("text"),
                },
            )

            last_event_for_reply = (
                _last_message_event(conversation_events)
                if send_ai_replies
                else None
            )
            pre_reply_state_key = ""
            pre_reply_already_sent = False
            skip_profile_for_reply = False
            candidate_needs_reply = bool(attempt.get("needs_reply_from_list"))
            if send_ai_replies:
                if last_event_for_reply is None:
                    skip_profile_for_reply = True
                elif last_event_for_reply.get("sender") == "self" and not force_reply:
                    skip_profile_for_reply = True
                elif duplicate_replied_chat_this_run:
                    skip_profile_for_reply = True
                elif not self_profile_text:
                    skip_profile_for_reply = True
                else:
                    pre_reply_state_key = _chat_reply_state_key(
                        profile_id,
                        chat_title,
                        last_event_for_reply,
                    )
                    pre_reply_already_sent = (
                        pre_reply_state_key in dict(_load_reply_state(config).get("sent") or {})
                        and not force_reply
                        and not candidate_needs_reply
                    )
                    if pre_reply_already_sent:
                        skip_profile_for_reply = True

            matched_profile_texts: list[str] = []
            profile_status = "not_attempted"
            if skip_profile_for_reply:
                profile_status = "skipped_reply_not_needed"
            else:
                profile_click_points = _matched_profile_click_points(
                    first_snapshot,
                    screen_size=screen_size,
                )
                profile_snapshots: list[dict[str, Any]] = []
                for profile_attempt_idx, header_point in enumerate(profile_click_points, start=1):
                    LOGGER.info(
                        "Opening matched profile attempt %s/%s at %s.",
                        profile_attempt_idx,
                        len(profile_click_points),
                        header_point,
                    )
                    human_gaussian_click(
                        device,
                        header_point[0],
                        header_point[1],
                        sigma_px=8.0,
                        max_offset_px=20,
                    )
                    time.sleep(random.uniform(1.0, 1.7))
                    profile_label = (
                        "matched_profile_initial"
                        if profile_attempt_idx == 1
                        else f"matched_profile_attempt_{profile_attempt_idx}"
                    )
                    current_profile_snapshot = _collect_visible_text_snapshot(
                        device,
                        label=profile_label,
                        output_dir=chat_dir,
                    )
                    _save_screen_if_requested(
                        device,
                        chat_dir / f"{profile_label}.png",
                        save_screenshots,
                    )
                    if _looks_like_bumble_non_chat_surface(
                        current_profile_snapshot["texts"],
                        current_profile_snapshot["activity"],
                    ):
                        profile_status = "profile_click_opened_non_chat_surface"
                        device.shell("input", "keyevent", "BACK", timeout=6, check=False)
                        time.sleep(random.uniform(0.45, 0.85))
                        continue
                    if _looks_like_bumble_chat_thread(
                        current_profile_snapshot["texts"],
                        current_profile_snapshot["activity"],
                    ):
                        profile_status = "header_click_stayed_in_thread"
                        continue

                    profile_snapshots = [current_profile_snapshot]
                    for scroll_idx in range(profile_scrolls):
                        LOGGER.info(
                            "Capturing matched profile scroll %s/%s.",
                            scroll_idx + 1,
                            profile_scrolls,
                        )
                        _scroll_matched_profile_lower(device, screen_size, scroll_idx)
                        time.sleep(random.uniform(0.6, 1.1))
                        scroll_label = f"matched_profile_scroll_{scroll_idx + 1}"
                        profile_snapshots.append(
                            _collect_visible_text_snapshot(
                                device,
                                label=scroll_label,
                                output_dir=chat_dir,
                            )
                        )
                        _save_screen_if_requested(
                            device,
                            chat_dir / f"{scroll_label}.png",
                            save_screenshots,
                        )
                    matched_profile_texts = _dedupe_strings(
                        text
                        for snapshot in profile_snapshots
                        for text in snapshot["texts"]
                    )
                    _write_text_lines(chat_dir / "matched_profile_texts.txt", matched_profile_texts)
                    profile_status = "captured"
                    break

            _append_reply_monitor_event(
                monitor_path,
                {
                    "event": "matched_profile_checked",
                    "profile_id": profile_id,
                    "chat_title": chat_title,
                    "chat_dir": str(chat_dir),
                    "profile_status": profile_status,
                    "matched_profile_text_count": len(matched_profile_texts),
                    "matched_profile_file": (
                        str(chat_dir / "matched_profile_texts.txt")
                        if matched_profile_texts
                        else ""
                    ),
                },
            )

            reply_result: dict[str, Any] = {"status": "not_requested"}
            if send_ai_replies:
                reply_result = {"status": "not_started"}
                last_event = last_event_for_reply
                if last_event is None:
                    reply_result = {"status": "skipped_no_visible_messages"}
                elif last_event.get("sender") == "self" and not force_reply:
                    skip_cleanup = _collect_visible_text_snapshot(
                        device,
                        label="skipped_last_self_before_cleanup",
                        output_dir=chat_dir,
                    )
                    draft_before = _chat_input_text_from_snapshot(skip_cleanup)
                    draft_after = ""
                    if draft_before:
                        skip_cleanup = _clear_chat_input_text(
                            device,
                            snapshot=skip_cleanup,
                            screen_size=screen_size,
                            chat_dir=chat_dir,
                            label="skipped_last_self_after_cleanup",
                        )
                        draft_after = _chat_input_text_from_snapshot(skip_cleanup)
                    reply_result = {
                        "status": "skipped_last_message_is_self",
                        "last_message": last_event,
                        "cleared_draft": draft_before,
                        "draft_after_cleanup": draft_after,
                    }
                elif duplicate_replied_chat_this_run:
                    reply_result = {
                        "status": "skipped_duplicate_chat_this_run",
                        "last_message": last_event,
                        "reason": "This chat already received a verified reply earlier in the same run.",
                    }
                elif not self_profile_text:
                    reply_result = {"status": "skipped_missing_self_profile"}
                else:
                    try:
                        reply_state = _load_reply_state(config)
                        state_key = pre_reply_state_key or _chat_reply_state_key(
                            profile_id,
                            chat_title,
                            last_event,
                        )
                        if (
                            pre_reply_already_sent
                            or (
                                state_key in dict(reply_state.get("sent") or {})
                                and not force_reply
                                and not candidate_needs_reply
                            )
                        ):
                            reply_result = {
                                "status": "skipped_already_replied_to_last_message",
                                "state_key": state_key,
                            }
                        else:
                            before_reply = _return_to_bumble_chat_thread(
                                device,
                                output_dir=chat_dir,
                                label="reply_before_ai",
                            )
                            if not _looks_like_bumble_chat_thread(
                                before_reply["texts"],
                                before_reply["activity"],
                            ):
                                reply_result = {
                                    "status": "failed_not_in_chat_before_reply",
                                    "activity": before_reply["activity"],
                                }
                            else:
                                _append_reply_monitor_event(
                                    monitor_path,
                                    {
                                        "event": "ai_call_started",
                                        "profile_id": profile_id,
                                        "chat_title": chat_title,
                                        "chat_dir": str(chat_dir),
                                        "last_message": last_event,
                                        "self_profile_line_count": len(
                                            [
                                                line
                                                for line in self_profile_text.splitlines()
                                                if line.strip()
                                            ]
                                        ),
                                        "customer_profile_line_count": len(
                                            [
                                                line
                                                for line in "\n".join(
                                                    matched_profile_texts
                                                ).splitlines()
                                                if line.strip()
                                            ]
                                        ),
                                        "conversation_file": str(
                                            chat_dir / "conversation_events.txt"
                                        ),
                                        "ai_input_context_file": str(
                                            chat_dir / "ai_input_context.json"
                                        ),
                                    },
                                )
                                ai_result = _call_bumble_reply_ai(
                                    config,
                                    self_profile_text=self_profile_text,
                                    customer_profile_text="\n".join(matched_profile_texts),
                                    conversation_text=_conversation_events_to_text(conversation_events),
                                    chat_title=chat_title,
                                    image_path=chat_dir / "matched_profile_initial.png",
                                    target_location=target_location,
                                    heat_threshold=heat_threshold,
                                    output_dir=chat_dir,
                                )
                                _append_reply_monitor_event(
                                    monitor_path,
                                    {
                                        "event": "ai_call_completed",
                                        "profile_id": profile_id,
                                        "chat_title": chat_title,
                                        "chat_dir": str(chat_dir),
                                        "reply": ai_result.get("reply"),
                                        "reply_parts": ai_result.get("reply_parts"),
                                        "heat_score": ai_result.get("heat_score"),
                                        "should_ask_ws": ai_result.get("should_ask_ws"),
                                        "reason": ai_result.get("reason"),
                                        "ai_response_file": str(chat_dir / "ai_response.json"),
                                        "ai_reply_file": str(chat_dir / "ai_reply.json"),
                                    },
                                )
                                raw_reply_parts = [
                                    str(part).strip()
                                    for part in ai_result.get("reply_parts", [])
                                    if str(part).strip()
                                ]
                                if (
                                    not config.USE_ADB_KEYBOARD
                                    and any(_has_non_ascii_letter_or_number(part) for part in raw_reply_parts)
                                ):
                                    fallback_reply = _ascii_fallback_reply_for_non_ascii_message(
                                        str(last_event.get("text") or "")
                                    )
                                    ai_result = {
                                        **ai_result,
                                        "unicode_fallback_reply": fallback_reply,
                                        "unicode_fallback_reason": (
                                            "ADBKeyboard was not available; using an ASCII fallback instead of skipping."
                                        ),
                                    }
                                    raw_reply_parts = [fallback_reply]
                                reply_parts = [
                                    _prepare_reply_for_device_text(
                                        part,
                                        allow_unicode=bool(config.USE_ADB_KEYBOARD),
                                    )
                                    for part in raw_reply_parts
                                ]
                                reply_parts = [part for part in reply_parts if part]
                                max_reply_parts = _clamp(
                                    int(getattr(config, "BUMBLE_MAX_REPLY_PARTS", 1) or 1),
                                    1,
                                    3,
                                )
                                reply_parts = reply_parts[:max_reply_parts]
                                if reply_result.get("status") == "skipped_unicode_reply_unsupported":
                                    pass
                                elif not reply_parts:
                                    reply_result = {
                                        "status": "skipped_ai_empty_reply",
                                        "ai": ai_result,
                                    }
                                else:
                                    _append_reply_monitor_event(
                                        monitor_path,
                                        {
                                            "event": "send_started",
                                            "profile_id": profile_id,
                                            "chat_title": chat_title,
                                            "chat_dir": str(chat_dir),
                                            "reply_parts": reply_parts,
                                        },
                                    )
                                    send_result: dict[str, Any] | None = None
                                    send_error: Exception | None = None
                                    for send_retry in range(1, 3):
                                        try:
                                            if send_retry > 1:
                                                _append_reply_monitor_event(
                                                    monitor_path,
                                                    {
                                                        "event": "send_retry_started",
                                                        "profile_id": profile_id,
                                                        "chat_title": chat_title,
                                                        "chat_dir": str(chat_dir),
                                                        "retry": send_retry,
                                                    },
                                                )
                                                time.sleep(random.uniform(0.7, 1.3))
                                            send_result = _send_bumble_reply_parts(
                                                device,
                                                screen_size=screen_size,
                                                reply_parts=reply_parts,
                                                chat_dir=chat_dir,
                                            )
                                            break
                                        except Exception as exc:
                                            send_error = exc
                                            _append_reply_monitor_event(
                                                monitor_path,
                                                {
                                                    "event": "send_retry_failed",
                                                    "profile_id": profile_id,
                                                    "chat_title": chat_title,
                                                    "chat_dir": str(chat_dir),
                                                    "retry": send_retry,
                                                    "error": str(exc),
                                                },
                                            )
                                            if send_retry >= 2:
                                                raise
                                    if send_result is None:
                                        raise ADBCommandError(
                                            f"Reply send did not return a result: {send_error}"
                                        )
                                    if not bool(send_result.get("verified_visible")):
                                        reply_result = {
                                            "status": "failed_unverified_send",
                                            "ai": ai_result,
                                            "send": send_result,
                                            "state_key": state_key,
                                        }
                                    else:
                                        reply_result = {
                                            "status": "sent",
                                            "ai": ai_result,
                                            "send": send_result,
                                            "state_key": state_key,
                                        }
                                        reply_state.setdefault("sent", {})[state_key] = {
                                            "profile_id": profile_id,
                                            "chat_title": chat_title,
                                            "last_message": last_event,
                                            "reply_parts": reply_parts,
                                            "sent_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                                        }
                                        _save_reply_state(reply_state)
                                        if chat_title_run_key:
                                            replied_chat_titles_this_run.add(chat_title_run_key)
                                    _append_reply_monitor_event(
                                        monitor_path,
                                        {
                                            "event": "send_completed",
                                            "profile_id": profile_id,
                                            "chat_title": chat_title,
                                            "chat_dir": str(chat_dir),
                                            "reply_status": reply_result.get("status"),
                                            "verified_visible": bool(
                                                send_result.get("verified_visible")
                                            ),
                                            "sent_parts": send_result.get("sent_parts"),
                                            "final_input_text": send_result.get(
                                                "final_input_text"
                                            ),
                                        },
                                    )
                    except Exception as exc:
                        LOGGER.exception("Reply flow failed for chat %s on %s.", chat_title, profile_id)
                        reply_result = {
                            "status": "failed_reply_exception",
                            "chat_title": chat_title,
                            "error": str(exc),
                        }
                        _append_reply_monitor_event(
                            monitor_path,
                            {
                                "event": "reply_exception",
                                "profile_id": profile_id,
                                "chat_title": chat_title,
                                "chat_dir": str(chat_dir),
                                "error": str(exc),
                            },
                        )

            opened_result = {
                "attempt": attempt,
                "chat_dir": str(chat_dir),
                "opened_thread_marker": opened,
                "chat_title": chat_title,
                "conversation_text_count": len(conversation_texts),
                "conversation_message_text_count": len(conversation_message_texts),
                "matched_profile_status": profile_status,
                "matched_profile_text_count": len(matched_profile_texts),
                "reply": reply_result,
                "conversation_texts_file": str(chat_dir / "conversation_texts.txt"),
                "conversation_messages_file": str(chat_dir / "conversation_messages.txt"),
                "matched_profile_texts_file": (
                    str(chat_dir / "matched_profile_texts.txt")
                    if matched_profile_texts
                    else ""
                ),
            }
            audit_last_event = (
                reply_result.get("last_message")
                if isinstance(reply_result, dict)
                else None
            )
            if not isinstance(audit_last_event, dict):
                audit_last_event = last_visible_event_for_monitor or last_event_for_reply or {}
            audit_ai = reply_result.get("ai") if isinstance(reply_result, dict) else {}
            audit_send = reply_result.get("send") if isinstance(reply_result, dict) else {}
            audit_reply_text = ""
            if isinstance(audit_ai, dict):
                audit_reply_text = str(audit_ai.get("reply") or "")
            if not audit_reply_text and isinstance(audit_send, dict):
                audit_reply_text = " | ".join(str(part) for part in audit_send.get("sent_parts") or [])
            audit_reason = ""
            if isinstance(reply_result, dict):
                audit_reason = str(reply_result.get("reason") or reply_result.get("error") or "")
            mark_attempt_audit(
                attempt,
                status=str(reply_result.get("status") if isinstance(reply_result, dict) else "unknown"),
                reason=audit_reason,
                chat_title=chat_title,
                chat_dir=str(chat_dir),
                last_message_sender=str(audit_last_event.get("sender") or ""),
                last_message_text=str(audit_last_event.get("text") or ""),
                reply_text=audit_reply_text,
                verified_visible=(
                    bool(audit_send.get("verified_visible"))
                    if isinstance(audit_send, dict)
                    else False
                ),
                matched_profile_status=profile_status,
            )
            (chat_dir / "chat_summary.json").write_text(
                json.dumps(opened_result, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            _append_reply_monitor_event(
                monitor_path,
                {
                    "event": "chat_result",
                    "profile_id": profile_id,
                    "chat_title": chat_title,
                    "chat_dir": str(chat_dir),
                    "reply_status": reply_result.get("status"),
                    "matched_profile_status": profile_status,
                    "matched_profile_text_count": len(matched_profile_texts),
                    "opened_chats_so_far": len(opened_results) + 1,
                },
            )
            opened_results.append(opened_result)

            if stop_phone_after_run and len(opened_results) >= max_chats:
                break

            device.shell("input", "keyevent", "BACK", timeout=6, check=False)
            time.sleep(random.uniform(0.35, 0.75))
            device.shell("input", "keyevent", "BACK", timeout=6, check=False)
            time.sleep(random.uniform(0.35, 0.75))
            open_bumble_tab(device, "chats")

        for item in reply_audit:
            if str(item.get("status") or "") != "pending_not_opened":
                continue
            if bool(item.get("needs_reply_from_list")):
                item.update(
                    {
                        "status": "not_processed",
                        "reason": (
                            "candidate was visible as needing reply but was not opened "
                            f"within max_chats={max_chats}"
                        ),
                        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    }
                )
            else:
                item.update(
                    {
                        "status": "not_needed_from_list",
                        "reason": "chat row did not show your move or unread marker in the list",
                        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    }
                )
        reply_audit_paths = _write_reply_audit_files(output_dir, reply_audit)
        unreplied_audit = _reply_audit_needs_attention(reply_audit)
        summary.update(
            {
                "status": "completed" if opened_results else "no_chat_opened",
                "opened_chats": len(opened_results),
                "chats": opened_results,
                "reply_audit_file": reply_audit_paths["json"],
                "reply_audit_text_file": reply_audit_paths["txt"],
                "unreplied_audit_count": len(unreplied_audit),
                "unreplied_audit": unreplied_audit[:20],
                "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
        )
        summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _append_reply_monitor_event(
            monitor_path,
            {
                "event": "phone_run_completed",
                "profile_id": profile_id,
                "status": summary["status"],
                "opened_chats": len(opened_results),
                "reply_statuses": _summarize_chat_reply_results(summary).get(
                    "reply_statuses"
                ),
                "unreplied_audit_count": len(unreplied_audit),
                "reply_audit_file": reply_audit_paths["json"],
            },
        )
        return summary
    except Exception as exc:
        summary.update(
            {
                "status": "failed",
                "error": str(exc),
                "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
        )
        summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _append_reply_monitor_event(
            monitor_path,
            {
                "event": "phone_run_failed",
                "profile_id": profile_id,
                "status": "failed",
                "error": str(exc),
            },
        )
        raise
    finally:
        appium.quit()
        if stop_phone_after_run:
            try:
                GeelarkOpenAPIClient(config).stop_phone([profile_id])
                LOGGER.info("Stopped cloud phone %s after chat capture.", profile_id)
                _append_reply_monitor_event(
                    monitor_path,
                    {
                        "event": "phone_stop_requested",
                        "profile_id": profile_id,
                        "status": "requested",
                    },
                )
            except Exception:
                LOGGER.debug("Failed to stop cloud phone %s.", profile_id, exc_info=True)
                _append_reply_monitor_event(
                    monitor_path,
                    {
                        "event": "phone_stop_failed",
                        "profile_id": profile_id,
                    },
                )


def _write_run_summary(
    path: Path,
    *,
    profile_id: str,
    output_dir: Path,
    target_likes: int,
    successful_likes: int,
    iteration_errors: int,
    popup_actions: int,
    daily_count: int,
    daily_limit: int,
    skip_capture: bool,
    status: str,
) -> None:
    summary = {
        "profile_id": profile_id,
        "output_dir": str(output_dir),
        "target_likes": target_likes,
        "successful_likes": successful_likes,
        "iteration_errors": iteration_errors,
        "popup_actions": popup_actions,
        "daily_count": daily_count,
        "daily_limit": daily_limit,
        "skip_capture": skip_capture,
        "status": status,
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def list_geelark_groups(config: Config) -> list[dict[str, Any]]:
    api = GeelarkOpenAPIClient(config)
    page = 1
    page_size = 100
    groups: list[dict[str, Any]] = []
    seen: set[str] = set()
    while True:
        payload = api.group_list(page=page, page_size=page_size)
        data = payload.get("data") or {}
        items = data.get("list") or []
        for item in items:
            group_id = str(item.get("id") or "")
            if not group_id or group_id in seen:
                continue
            seen.add(group_id)
            groups.append(dict(item))
        total = int(data.get("total") or len(groups))
        if not items or len(groups) >= total:
            break
        page += 1
    return groups


def list_geelark_phones(config: Config) -> list[dict[str, Any]]:
    api = GeelarkOpenAPIClient(config)
    page = 1
    page_size = 100
    phones: list[dict[str, Any]] = []
    seen: set[str] = set()
    while True:
        payload = api.phone_list(page=page, page_size=page_size)
        data = payload.get("data") or {}
        items = data.get("items") or []
        for item in items:
            phone_id = str(item.get("id") or "")
            if not phone_id or phone_id in seen:
                continue
            seen.add(phone_id)
            phones.append(dict(item))
        total = int(data.get("total") or len(phones))
        if not items or len(phones) >= total:
            break
        page += 1
    return phones


def _find_group(config: Config, *, group_id: str = "", group_name: str = "") -> dict[str, Any]:
    groups = list_geelark_groups(config)
    if group_id:
        for group in groups:
            if str(group.get("id")) == str(group_id):
                return group
        raise AutomationError(f"GeeLark group id was not found: {group_id}")
    if group_name:
        matches = [group for group in groups if str(group.get("name")) == group_name]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise AutomationError(f"Multiple GeeLark groups have name: {group_name}")
        raise AutomationError(f"GeeLark group name was not found: {group_name}")
    raise AutomationError("Missing group id or group name.")


def _phone_public_summary(phone: dict[str, Any]) -> dict[str, Any]:
    group = phone.get("group") or {}
    return {
        "id": str(phone.get("id") or ""),
        "serialName": phone.get("serialName"),
        "serialNo": phone.get("serialNo"),
        "status": phone.get("status"),
        "group": {
            "id": str(group.get("id") or ""),
            "name": group.get("name"),
        },
    }


def run_bumble_group_right_swipe_loop(
    config: Config,
    *,
    group_id: str = "",
    group_name: str = "",
    per_phone_min_count: int | None = None,
    per_phone_max_count: int | None = 1,
    per_phone_until_daily_limit: bool = False,
    max_phones: int | None = None,
    start_offset: int = 0,
    prepare_adb: bool = False,
    use_api_shell: bool = True,
    start_tab: str = "people",
    skip_capture: bool = True,
    view_profile_probability: float | None = None,
    view_profile_max_swipes: int | None = None,
    popup_check_every_n_actions: int | None = None,
    stop_phone_after_run: bool | None = None,
    prestart_phones: bool = False,
    economy_mode: bool = False,
    capture_self_profile: bool = False,
    output_root: Path | None = None,
) -> dict[str, Any]:
    if economy_mode:
        config = replace(
            config,
            RANDOM_WAIT_MIN_SECONDS=0.55,
            RANDOM_WAIT_MAX_SECONDS=1.25,
            PRE_LIKE_WAIT_MIN_SECONDS=0.10,
            PRE_LIKE_WAIT_MAX_SECONDS=0.35,
            BUMBLE_OPEN_WAIT_MIN_SECONDS=1.2,
            BUMBLE_OPEN_WAIT_MAX_SECONDS=2.0,
            BUMBLE_TAB_WAIT_MIN_SECONDS=0.75,
            BUMBLE_TAB_WAIT_MAX_SECONDS=1.35,
            PROFILE_VIEW_PROBABILITY=0.0,
            PROFILE_VIEW_MAX_SWIPES=1,
            POPUP_CHECK_EVERY_N_ACTIONS=0,
        )

    if per_phone_min_count is not None and per_phone_min_count <= 0:
        raise AutomationError("--per-phone-min-count must be greater than 0.")
    if per_phone_max_count is not None and per_phone_max_count <= 0:
        raise AutomationError("--per-phone-max-count must be greater than 0.")
    if per_phone_max_count is None and not per_phone_until_daily_limit:
        raise AutomationError("Provide --per-phone-max-count or --per-phone-until-daily-limit.")
    if (
        per_phone_min_count is not None
        and per_phone_max_count is not None
        and per_phone_min_count > per_phone_max_count
    ):
        raise AutomationError("--per-phone-min-count cannot be greater than --per-phone-max-count.")

    group = _find_group(config, group_id=group_id, group_name=group_name)
    resolved_group_id = str(group.get("id") or "")
    resolved_group_name = str(group.get("name") or "")
    all_phones = list_geelark_phones(config)
    phones = [
        phone
        for phone in all_phones
        if str((phone.get("group") or {}).get("id") or "") == resolved_group_id
    ]
    if start_offset > 0:
        phones = phones[start_offset:]
    if max_phones is not None:
        phones = phones[:max_phones]

    output_dir = (
        output_root
        if output_root is not None
        else Path.cwd()
        / "diagnostics"
        / "group_runs"
        / resolved_group_id
        / _timestamp_for_path()
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    group_log = output_dir / "group_run_log.jsonl"
    group_summary_path = output_dir / "group_run_summary.json"

    stop_after = (
        config.STOP_PHONE_AFTER_GROUP_RUN
        if stop_phone_after_run is None
        else stop_phone_after_run
    )
    summary = {
        "group_id": resolved_group_id,
        "group_name": resolved_group_name,
        "output_dir": str(output_dir),
        "available_phones": len(
            [
                phone
                for phone in all_phones
                if str((phone.get("group") or {}).get("id") or "") == resolved_group_id
            ]
        ),
        "target_phones": len(phones),
        "processed_phones": 0,
        "successful_phones": 0,
        "failed_phones": 0,
        "successful_likes": 0,
        "per_phone_min_count": per_phone_min_count,
        "per_phone_max_count": per_phone_max_count,
        "per_phone_until_daily_limit": per_phone_until_daily_limit,
        "prestart_phones": prestart_phones,
        "economy_mode": economy_mode,
        "capture_self_profile": capture_self_profile,
        "skip_capture": skip_capture,
        "status": "running",
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    group_summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    LOGGER.info(
        "Group %s (%s) has %s cloud phone(s); target this run=%s.",
        resolved_group_name,
        resolved_group_id,
        summary["available_phones"],
        len(phones),
    )

    if prestart_phones and phones:
        ids = [str(phone.get("id")) for phone in phones if phone.get("id")]
        LOGGER.info("Pre-starting %s cloud phone(s) for this group run.", len(ids))
        api = GeelarkOpenAPIClient(config)
        for start in range(0, len(ids), 20):
            api.start_phone(ids[start : start + 20])
        time.sleep(12.0)

    for index, phone in enumerate(phones, start=1 + start_offset):
        phone_summary = _phone_public_summary(phone)
        phone_id = phone_summary["id"]
        serial = str(phone_summary.get("serialNo") or index)
        serial_name = str(phone_summary.get("serialName") or "phone")
        safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{serial}_{serial_name}_{phone_id}")
        phone_output_dir = output_dir / f"{index:03d}_{safe_label}"
        log_entry: dict[str, Any] = {
            "index": index,
            "phone": phone_summary,
            "event": "phone_started",
            "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        with group_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

        try:
            selected_count = per_phone_max_count
            if (
                not per_phone_until_daily_limit
                and per_phone_min_count is not None
                and per_phone_max_count is not None
            ):
                selected_count = random.randint(per_phone_min_count, per_phone_max_count)
            LOGGER.info(
                "Running phone %s/%s (%s) with target likes=%s.",
                index,
                start_offset + len(phones),
                serial_name,
                "daily-limit" if per_phone_until_daily_limit else selected_count,
            )
            run_summary = run_bumble_right_swipe_loop(
                config,
                profile_id=phone_id,
                max_count=selected_count,
                prepare_adb=prepare_adb,
                use_api_shell=use_api_shell,
                start_tab=start_tab,
                output_root=phone_output_dir,
                skip_capture=skip_capture,
                view_profile_probability=view_profile_probability,
                view_profile_max_swipes=view_profile_max_swipes,
                until_daily_limit=per_phone_until_daily_limit,
                popup_check_every_n_actions=popup_check_every_n_actions,
                stop_phone_after_run=stop_after,
                capture_self_profile=capture_self_profile,
                phone_metadata=phone_summary,
            )
            successful_likes = int(run_summary.get("successful_likes", 0))
            summary["successful_phones"] = int(summary["successful_phones"]) + 1
            summary["successful_likes"] = int(summary["successful_likes"]) + successful_likes
            result_entry = {
                "index": index,
                "phone": phone_summary,
                "event": "phone_completed",
                "successful_likes": successful_likes,
                "run_summary": run_summary,
                "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
        except Exception as exc:
            summary["failed_phones"] = int(summary["failed_phones"]) + 1
            result_entry = {
                "index": index,
                "phone": phone_summary,
                "event": "phone_failed",
                "error": str(exc),
                "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
            LOGGER.exception("Group phone run failed for %s (%s).", phone_id, serial_name)

        summary["processed_phones"] = int(summary["processed_phones"]) + 1
        summary["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        summary["status"] = (
            "completed"
            if int(summary["processed_phones"]) >= int(summary["target_phones"])
            else "running"
        )
        with group_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(result_entry, ensure_ascii=False) + "\n")
        group_summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        if int(summary["processed_phones"]) < int(summary["target_phones"]):
            time.sleep(random.uniform(2.5, 6.0))

    return summary


def _summarize_chat_reply_results(run_summary: dict[str, Any]) -> dict[str, Any]:
    result = {
        "opened_chats": int(run_summary.get("opened_chats") or 0),
        "replies_sent": 0,
        "replies_skipped": 0,
        "reply_failures": 0,
        "unverified_sends": 0,
        "unreplied_audit_count": int(run_summary.get("unreplied_audit_count") or 0),
        "reply_statuses": {},
    }
    statuses: dict[str, int] = {}
    for chat in run_summary.get("chats") or []:
        if not isinstance(chat, dict):
            continue
        reply = chat.get("reply")
        if not isinstance(reply, dict):
            continue
        status = str(reply.get("status") or "unknown")
        statuses[status] = statuses.get(status, 0) + 1
        if status == "sent":
            result["replies_sent"] = int(result["replies_sent"]) + 1
            send_result = reply.get("send")
            if isinstance(send_result, dict) and not bool(send_result.get("verified_visible")):
                result["unverified_sends"] = int(result["unverified_sends"]) + 1
        elif status.startswith("failed"):
            result["reply_failures"] = int(result["reply_failures"]) + 1
        elif status != "not_requested":
            result["replies_skipped"] = int(result["replies_skipped"]) + 1
    result["reply_statuses"] = statuses
    return result


def _apply_group_reply_counts(summary: dict[str, Any], reply_counts: dict[str, Any]) -> None:
    summary["successful_phones"] = int(summary["successful_phones"]) + 1
    summary["opened_chats"] = int(summary["opened_chats"]) + int(reply_counts["opened_chats"])
    summary["replies_sent"] = int(summary["replies_sent"]) + int(reply_counts["replies_sent"])
    summary["replies_skipped"] = int(summary["replies_skipped"]) + int(reply_counts["replies_skipped"])
    summary["reply_failures"] = int(summary["reply_failures"]) + int(reply_counts["reply_failures"])
    summary["unverified_sends"] = int(summary["unverified_sends"]) + int(reply_counts["unverified_sends"])
    summary["unreplied_audit_count"] = int(summary["unreplied_audit_count"]) + int(
        reply_counts.get("unreplied_audit_count") or 0
    )
    if (
        int(reply_counts["reply_failures"]) > 0
        or int(reply_counts["unverified_sends"]) > 0
        or int(reply_counts.get("unreplied_audit_count") or 0) > 0
    ):
        summary["problem_phones"] = int(summary["problem_phones"]) + 1
    merged_statuses = dict(summary.get("reply_statuses") or {})
    for status, count in dict(reply_counts["reply_statuses"]).items():
        merged_statuses[status] = int(merged_statuses.get(status, 0)) + int(count)
    summary["reply_statuses"] = merged_statuses


def _should_retry_group_chat_with_api_shell(exc: Exception) -> bool:
    lowered = str(exc).lower()
    markers = (
        "you should run glogin to login first",
        "requires glogin",
        "bumble main activity did not reach foreground",
        "activity=",
        "adb device did not become ready",
        "device offline",
        "device not found",
        "no devices/emulators found",
    )
    return any(marker in lowered for marker in markers)


def run_bumble_group_reply_chats(
    config: Config,
    *,
    group_id: str = "",
    group_name: str = "",
    max_phones: int | None = None,
    start_offset: int = 0,
    max_chats_per_phone: int = 3,
    chat_scrolls: int = 0,
    profile_scrolls: int = 2,
    prepare_adb: bool = False,
    use_api_shell: bool = False,
    stop_phone_after_run: bool | None = None,
    economy_mode: bool = True,
    save_screenshots: bool = True,
    capture_missing_self_profile: bool = True,
    target_location: str = "unknown",
    heat_threshold: int = 70,
    force_reply: bool = False,
    inter_phone_wait_min_seconds: float = 1.0,
    inter_phone_wait_max_seconds: float = 3.5,
    per_phone_timeout_seconds: float | None = None,
    output_root: Path | None = None,
) -> dict[str, Any]:
    if max_chats_per_phone <= 0:
        raise AutomationError("--max-chats-per-phone must be greater than 0.")
    if chat_scrolls < 0 or profile_scrolls < 0:
        raise AutomationError("--chat-scrolls and --profile-scrolls must be >= 0.")
    if start_offset < 0:
        raise AutomationError("--start-offset must be >= 0.")
    if max_phones is not None and max_phones <= 0:
        raise AutomationError("--max-phones must be greater than 0.")
    if inter_phone_wait_min_seconds < 0 or inter_phone_wait_max_seconds < 0:
        raise AutomationError("inter-phone waits must be >= 0.")
    if inter_phone_wait_min_seconds > inter_phone_wait_max_seconds:
        raise AutomationError("--inter-phone-wait-min cannot exceed --inter-phone-wait-max.")
    if per_phone_timeout_seconds is not None and per_phone_timeout_seconds <= 0:
        raise AutomationError("--per-phone-timeout-seconds must be greater than 0.")

    group = _find_group(config, group_id=group_id, group_name=group_name)
    resolved_group_id = str(group.get("id") or "")
    resolved_group_name = str(group.get("name") or "")
    all_phones = list_geelark_phones(config)
    group_phones = [
        phone
        for phone in all_phones
        if str((phone.get("group") or {}).get("id") or "") == resolved_group_id
    ]
    phones = group_phones[start_offset:]
    if max_phones is not None:
        phones = phones[:max_phones]

    output_dir = (
        output_root
        if output_root is not None
        else Path.cwd()
        / "diagnostics"
        / "group_chat_replies"
        / resolved_group_id
        / _timestamp_for_path()
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    group_log = output_dir / "group_chat_reply_log.jsonl"
    group_summary_path = output_dir / "group_chat_reply_summary.json"

    stop_after = (
        config.STOP_PHONE_AFTER_GROUP_RUN
        if stop_phone_after_run is None
        else stop_phone_after_run
    )
    summary: dict[str, Any] = {
        "group_id": resolved_group_id,
        "group_name": resolved_group_name,
        "output_dir": str(output_dir),
        "available_phones": len(group_phones),
        "target_phones": len(phones),
        "processed_phones": 0,
        "successful_phones": 0,
        "failed_phones": 0,
        "problem_phones": 0,
        "opened_chats": 0,
        "replies_sent": 0,
        "replies_skipped": 0,
        "reply_failures": 0,
        "unverified_sends": 0,
        "unreplied_audit_count": 0,
        "reply_statuses": {},
        "max_chats_per_phone": max_chats_per_phone,
        "chat_scrolls": chat_scrolls,
        "profile_scrolls": profile_scrolls,
        "economy_mode": economy_mode,
        "save_screenshots": save_screenshots,
        "capture_missing_self_profile": capture_missing_self_profile,
        "target_location": target_location,
        "heat_threshold": heat_threshold,
        "force_reply": force_reply,
        "per_phone_timeout_seconds": per_phone_timeout_seconds,
        "status": "running",
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    group_summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    LOGGER.info(
        "Reply group %s (%s) has %s cloud phone(s); target this run=%s.",
        resolved_group_name,
        resolved_group_id,
        len(group_phones),
        len(phones),
    )

    for index, phone in enumerate(phones, start=1 + start_offset):
        phone_summary = _phone_public_summary(phone)
        phone_id = phone_summary["id"]
        serial = str(phone_summary.get("serialNo") or index)
        serial_name = str(phone_summary.get("serialName") or "phone")
        safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{serial}_{serial_name}_{phone_id}")
        phone_output_dir = output_dir / f"{index:03d}_{safe_label}"
        start_entry = {
            "index": index,
            "phone": phone_summary,
            "event": "phone_started",
            "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        with group_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(start_entry, ensure_ascii=False) + "\n")

        try:
            LOGGER.info(
                "Running chat reply capture for phone %s/%s (%s), max chats=%s.",
                index,
                start_offset + len(phones),
                serial_name,
                max_chats_per_phone,
            )
            timeout_message = (
                f"Phone {phone_id} exceeded {per_phone_timeout_seconds:.0f}s without completing reply capture."
                if per_phone_timeout_seconds
                else ""
            )
            with _PhoneRunDeadline(per_phone_timeout_seconds, timeout_message):
                run_summary = run_bumble_chat_capture(
                    config,
                    profile_id=phone_id,
                    prepare_adb=prepare_adb,
                    use_api_shell=use_api_shell,
                    output_root=phone_output_dir,
                    max_chats=max_chats_per_phone,
                    chat_scrolls=chat_scrolls,
                    profile_scrolls=profile_scrolls,
                    stop_phone_after_run=stop_after,
                    economy_mode=economy_mode,
                    phone_metadata=phone_summary,
                    save_screenshots=save_screenshots,
                    list_only=False,
                    send_ai_replies=True,
                    capture_self_profile_for_ai=capture_missing_self_profile,
                    target_location=target_location,
                    heat_threshold=heat_threshold,
                    force_reply=force_reply,
                    allow_global_self_profile_fallback=False,
            )
            reply_counts = _summarize_chat_reply_results(run_summary)
            _apply_group_reply_counts(summary, reply_counts)
            result_entry = {
                "index": index,
                "phone": phone_summary,
                "event": "phone_completed",
                "reply_counts": reply_counts,
                "run_summary": run_summary,
                "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
        except PhoneRunTimeout as exc:
            summary["failed_phones"] = int(summary["failed_phones"]) + 1
            result_entry = {
                "index": index,
                "phone": phone_summary,
                "event": "phone_timeout",
                "error": str(exc),
                "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
            LOGGER.warning("Group chat reply run timed out for %s (%s): %s", phone_id, serial_name, exc)
        except Exception as exc:
            if not use_api_shell and _should_retry_group_chat_with_api_shell(exc):
                LOGGER.warning(
                    "Local ADB chat reply run failed for %s (%s): %s; retrying with GeeLark API shell.",
                    phone_id,
                    serial_name,
                    exc,
                )
                try:
                    retry_summary = run_bumble_chat_capture(
                        config,
                        profile_id=phone_id,
                        prepare_adb=prepare_adb,
                        use_api_shell=True,
                        output_root=phone_output_dir / "api_shell_retry",
                        max_chats=max_chats_per_phone,
                        chat_scrolls=chat_scrolls,
                        profile_scrolls=profile_scrolls,
                        stop_phone_after_run=stop_after,
                        economy_mode=economy_mode,
                        phone_metadata=phone_summary,
                        save_screenshots=False,
                        list_only=False,
                        send_ai_replies=True,
                        capture_self_profile_for_ai=capture_missing_self_profile,
                        target_location=target_location,
                        heat_threshold=heat_threshold,
                        force_reply=force_reply,
                        allow_global_self_profile_fallback=False,
                    )
                    reply_counts = _summarize_chat_reply_results(retry_summary)
                    _apply_group_reply_counts(summary, reply_counts)
                    result_entry = {
                        "index": index,
                        "phone": phone_summary,
                        "event": "phone_completed_api_shell_retry",
                        "first_error": str(exc),
                        "reply_counts": reply_counts,
                        "run_summary": retry_summary,
                        "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    }
                except Exception as retry_exc:
                    summary["failed_phones"] = int(summary["failed_phones"]) + 1
                    result_entry = {
                        "index": index,
                        "phone": phone_summary,
                        "event": "phone_failed",
                        "error": str(retry_exc),
                        "first_error": str(exc),
                        "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    }
                    LOGGER.exception(
                        "API shell retry failed for %s (%s).",
                        phone_id,
                        serial_name,
                    )
            else:
                summary["failed_phones"] = int(summary["failed_phones"]) + 1
                result_entry = {
                    "index": index,
                    "phone": phone_summary,
                    "event": "phone_failed",
                    "error": str(exc),
                    "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                }
                LOGGER.exception("Group chat reply run failed for %s (%s).", phone_id, serial_name)

        summary["processed_phones"] = int(summary["processed_phones"]) + 1
        summary["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        summary["status"] = (
            "completed"
            if int(summary["processed_phones"]) >= int(summary["target_phones"])
            else "running"
        )
        with group_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(result_entry, ensure_ascii=False) + "\n")
        group_summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        if int(summary["processed_phones"]) < int(summary["target_phones"]):
            wait_s = random.uniform(
                inter_phone_wait_min_seconds,
                inter_phone_wait_max_seconds,
            )
            LOGGER.info("Waiting %.2fs before next cloud phone.", wait_s)
            time.sleep(wait_s)

    return summary


def configure_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="GeeLark multimodal automation framework")
    subparsers = parser.add_subparsers(dest="command")

    test_parser = subparsers.add_parser(
        "test-profile", help="extract current UI context and optionally swipe/draft/send"
    )
    test_parser.add_argument("--env-file", default="", help="optional .env file to load")
    test_parser.add_argument("--profile-id", default=os.getenv("GEELARK_PROFILE_ID", ""))
    test_parser.add_argument(
        "--prepare-adb",
        action="store_true",
        help="start the cloud phone if needed and enable ADB before connecting",
    )
    test_parser.add_argument(
        "--api-shell",
        action="store_true",
        help="use GeeLark shell/execute instead of local adb",
    )
    test_parser.add_argument("--swipe", action="store_true", help="execute one Bezier swipe")
    test_parser.add_argument(
        "--draft-reply", action="store_true", help="call AI and save a reply draft"
    )
    test_parser.add_argument(
        "--send",
        action="store_true",
        help="send the AI reply after drafting; omit this for safe dry-run testing",
    )
    test_parser.add_argument(
        "--no-screenshot", action="store_true", help="skip screenshot capture"
    )

    swipe_parser = subparsers.add_parser(
        "bumble-right-swipe",
        help="right-swipe Bumble profiles, optionally capturing visible profile/photo first",
    )
    swipe_parser.add_argument("--env-file", default="", help="optional .env file to load")
    swipe_parser.add_argument("--profile-id", default=os.getenv("GEELARK_PROFILE_ID", ""))
    swipe_parser.add_argument(
        "--max-count",
        type=int,
        default=None,
        help="maximum profiles to capture and right-swipe in this run",
    )
    swipe_parser.add_argument(
        "--until-daily-limit",
        action="store_true",
        help="continue until the remaining DAILY_ACTION_LIMIT quota is consumed",
    )
    swipe_parser.add_argument(
        "--prepare-adb",
        action="store_true",
        help="start the cloud phone if needed and enable ADB before connecting",
    )
    swipe_parser.add_argument(
        "--api-shell",
        action="store_true",
        help="use GeeLark shell/execute instead of local adb",
    )
    swipe_parser.add_argument(
        "--start-tab",
        choices=["discover", "people"],
        default="people",
        help="Bumble tab to open before the loop starts",
    )
    swipe_parser.add_argument(
        "--output-dir",
        default="",
        help="optional output directory for captured profiles",
    )
    swipe_parser.add_argument(
        "--skip-capture",
        action="store_true",
        help="do not read customer UI text or save customer photos; only log swipe metadata",
    )
    swipe_parser.add_argument(
        "--view-profile-probability",
        type=float,
        default=None,
        help="probability of doing extra vertical profile-view swipes before each like",
    )
    swipe_parser.add_argument(
        "--view-profile-max-swipes",
        type=int,
        default=None,
        help="maximum extra vertical profile-view swipes before each like",
    )
    swipe_parser.add_argument(
        "--popup-check-every-n-actions",
        type=int,
        default=None,
        help="check and handle common popups every N likes; 0 disables per-like checks",
    )

    chat_parser = subparsers.add_parser(
        "bumble-capture-chat",
        help="capture Bumble chat list, one or more matched chat threads, and visible matched profile text",
    )
    chat_parser.add_argument("--env-file", default="", help="optional .env file to load")
    chat_parser.add_argument("--profile-id", default=os.getenv("GEELARK_PROFILE_ID", ""))
    chat_parser.add_argument("--group-id", default="", help="GeeLark group id to pick one phone from")
    chat_parser.add_argument("--group-name", default="", help="GeeLark group name to pick one phone from")
    chat_parser.add_argument(
        "--start-offset",
        type=int,
        default=0,
        help="when using a group, pick the phone after skipping N phones",
    )
    chat_parser.add_argument(
        "--max-chats",
        type=int,
        default=1,
        help="maximum matched chat threads to open and capture",
    )
    chat_parser.add_argument(
        "--chat-scrolls",
        type=int,
        default=2,
        help="number of upward-history scrolls to capture inside the chat thread",
    )
    chat_parser.add_argument(
        "--profile-scrolls",
        type=int,
        default=4,
        help="number of profile-detail scrolls after tapping the chat header",
    )
    chat_parser.add_argument(
        "--prepare-adb",
        action="store_true",
        help="start the cloud phone if needed and enable ADB before connecting",
    )
    chat_parser.add_argument(
        "--api-shell",
        action="store_true",
        help="use GeeLark shell/execute instead of local adb",
    )
    chat_parser.add_argument(
        "--keep-phone-running",
        action="store_true",
        help="do not stop the cloud phone after capture",
    )
    chat_parser.add_argument(
        "--economy-mode",
        action="store_true",
        help="reduce Bumble open/tab waits for lower cloud-phone billed minutes",
    )
    chat_parser.add_argument(
        "--save-screenshots",
        action="store_true",
        help="save PNG screenshots for each captured chat/profile state",
    )
    chat_parser.add_argument(
        "--list-only",
        action="store_true",
        help="capture only the Bumble chat-list UI/screenshot and stop",
    )
    chat_parser.add_argument(
        "--send-ai-replies",
        action="store_true",
        help="call the AI reply endpoint and send replies to chats whose latest visible message is from the customer",
    )
    chat_parser.add_argument(
        "--capture-self-profile-for-ai",
        action="store_true",
        help="capture the account owner's Bumble profile before replying and include it in the AI context",
    )
    chat_parser.add_argument(
        "--target-location",
        default="unknown",
        help="customer location for local-time aware AI replies; use unknown when not available",
    )
    chat_parser.add_argument(
        "--heat-threshold",
        type=int,
        default=70,
        help="minimum heat score where AI may suggest moving to easier contact",
    )
    chat_parser.add_argument(
        "--force-reply",
        action="store_true",
        help="reply even if the latest visible message appears to be from self or this last message was already replied to",
    )
    chat_parser.add_argument(
        "--output-dir",
        default="",
        help="optional output directory for the chat capture",
    )

    group_swipe_parser = subparsers.add_parser(
        "bumble-group-right-swipe",
        help="run Bumble right-swipe automation across every cloud phone in a GeeLark group",
    )
    group_swipe_parser.add_argument("--env-file", default="", help="optional .env file to load")
    group_swipe_parser.add_argument("--group-id", default="", help="GeeLark group id")
    group_swipe_parser.add_argument("--group-name", default="", help="GeeLark group name")
    group_swipe_parser.add_argument(
        "--per-phone-min-count",
        type=int,
        default=None,
        help="minimum likes to perform on each cloud phone; combines with max for random count",
    )
    group_swipe_parser.add_argument(
        "--per-phone-max-count",
        type=int,
        default=None,
        help="maximum likes to perform on each cloud phone",
    )
    group_swipe_parser.add_argument(
        "--per-phone-until-daily-limit",
        action="store_true",
        help="continue each cloud phone until its own daily quota is consumed",
    )
    group_swipe_parser.add_argument(
        "--max-phones",
        type=int,
        default=None,
        help="optional cap on how many phones from the group to process",
    )
    group_swipe_parser.add_argument(
        "--start-offset",
        type=int,
        default=0,
        help="skip the first N phones in the filtered group list",
    )
    group_swipe_parser.add_argument(
        "--prepare-adb",
        action="store_true",
        help="start each cloud phone if needed and enable ADB before connecting",
    )
    group_swipe_parser.add_argument(
        "--api-shell",
        action="store_true",
        help="use GeeLark shell/execute instead of local adb",
    )
    group_swipe_parser.add_argument(
        "--start-tab",
        choices=["discover", "people"],
        default="people",
        help="Bumble tab to open before each phone loop starts",
    )
    group_swipe_parser.add_argument(
        "--skip-capture",
        action="store_true",
        help="do not read customer UI text or save customer photos; only log swipe metadata",
    )
    group_swipe_parser.add_argument(
        "--capture-self-profile",
        action="store_true",
        help="save the account owner's visible Bumble profile text before liking",
    )
    group_swipe_parser.add_argument(
        "--view-profile-probability",
        type=float,
        default=None,
        help="probability of doing extra vertical profile-view swipes before each like",
    )
    group_swipe_parser.add_argument(
        "--view-profile-max-swipes",
        type=int,
        default=None,
        help="maximum extra vertical profile-view swipes before each like",
    )
    group_swipe_parser.add_argument(
        "--popup-check-every-n-actions",
        type=int,
        default=None,
        help="check and handle common popups every N likes; 0 disables per-like checks",
    )
    group_swipe_parser.add_argument(
        "--keep-phone-running",
        action="store_true",
        help="do not stop each cloud phone after its run",
    )
    group_swipe_parser.add_argument(
        "--prestart-phones",
        action="store_true",
        help="batch-start target phones before processing to reduce per-phone startup waiting",
    )
    group_swipe_parser.add_argument(
        "--economy-mode",
        action="store_true",
        help="reduce waits and checks for lower cloud-phone billed minutes",
    )
    group_swipe_parser.add_argument(
        "--output-dir",
        default="",
        help="optional output directory for the group run logs",
    )

    group_reply_parser = subparsers.add_parser(
        "bumble-group-reply-chats",
        help="capture matched Bumble chats and send AI replies across every cloud phone in a GeeLark group",
    )
    group_reply_parser.add_argument("--env-file", default="", help="optional .env file to load")
    group_reply_parser.add_argument("--group-id", default="", help="GeeLark group id")
    group_reply_parser.add_argument("--group-name", default="", help="GeeLark group name")
    group_reply_parser.add_argument(
        "--max-phones",
        type=int,
        default=None,
        help="optional cap on how many phones from the group to process",
    )
    group_reply_parser.add_argument(
        "--start-offset",
        type=int,
        default=0,
        help="skip the first N phones in the filtered group list",
    )
    group_reply_parser.add_argument(
        "--max-chats-per-phone",
        type=int,
        default=3,
        help="maximum matched chat threads to inspect and reply to per phone",
    )
    group_reply_parser.add_argument(
        "--chat-scrolls",
        type=int,
        default=0,
        help="number of upward-history scrolls to capture inside each chat thread",
    )
    group_reply_parser.add_argument(
        "--profile-scrolls",
        type=int,
        default=2,
        help="number of profile-detail scrolls after tapping the chat header",
    )
    group_reply_parser.add_argument(
        "--prepare-adb",
        action="store_true",
        help="start each cloud phone if needed and enable ADB before connecting",
    )
    group_reply_parser.add_argument(
        "--api-shell",
        action="store_true",
        help="use GeeLark shell/execute instead of local adb",
    )
    group_reply_parser.add_argument(
        "--keep-phone-running",
        action="store_true",
        help="do not stop each cloud phone after its run",
    )
    group_reply_parser.add_argument(
        "--economy-mode",
        action="store_true",
        default=True,
        help="reduce Bumble open/tab waits for lower cloud-phone billed minutes",
    )
    group_reply_parser.add_argument(
        "--normal-waits",
        action="store_true",
        help="disable economy-mode wait reductions",
    )
    group_reply_parser.add_argument(
        "--no-screenshots",
        action="store_true",
        help="do not save chat/profile screenshots or send image context to AI",
    )
    group_reply_parser.add_argument(
        "--no-capture-missing-self-profile",
        action="store_true",
        help="do not capture own profile when no cached self profile exists",
    )
    group_reply_parser.add_argument(
        "--target-location",
        default="unknown",
        help="customer location for local-time aware AI replies; use unknown when not available",
    )
    group_reply_parser.add_argument(
        "--heat-threshold",
        type=int,
        default=70,
        help="minimum heat score where AI may suggest moving to easier contact",
    )
    group_reply_parser.add_argument(
        "--force-reply",
        action="store_true",
        help="reply even if the latest visible message appears to be from self or this last message was already replied to",
    )
    group_reply_parser.add_argument(
        "--inter-phone-wait-min",
        type=float,
        default=1.0,
        help="minimum random wait between cloud phones",
    )
    group_reply_parser.add_argument(
        "--inter-phone-wait-max",
        type=float,
        default=3.5,
        help="maximum random wait between cloud phones",
    )
    group_reply_parser.add_argument(
        "--per-phone-timeout-seconds",
        type=float,
        default=180.0,
        help="stop the current cloud phone if one phone run does not complete within this many seconds",
    )
    group_reply_parser.add_argument(
        "--monitor-loops",
        type=int,
        default=1,
        help="repeat the whole group reply scan this many times; 0 means run until interrupted",
    )
    group_reply_parser.add_argument(
        "--monitor-sleep-min",
        type=float,
        default=25.0,
        help="minimum random wait between repeated group reply scans",
    )
    group_reply_parser.add_argument(
        "--monitor-sleep-max",
        type=float,
        default=45.0,
        help="maximum random wait between repeated group reply scans",
    )
    group_reply_parser.add_argument(
        "--output-dir",
        default="",
        help="optional output directory for the group chat reply logs",
    )

    args = parser.parse_args()
    if getattr(args, "env_file", ""):
        load_env_file(Path(args.env_file), override=False)
    config = Config()

    try:
        if args.command == "test-profile":
            profile_id = args.profile_id or config.GEELARK_PROFILE_ID
            if not profile_id:
                raise AutomationError("Missing --profile-id or GEELARK_PROFILE_ID.")
            run_profile_test(
                config,
                profile_id=profile_id,
                prepare_adb=args.prepare_adb,
                use_api_shell=args.api_shell,
                do_swipe=args.swipe,
                draft_reply=args.draft_reply or args.send,
                send_reply=args.send,
                save_screenshot=not args.no_screenshot,
            )
            return

        if args.command == "bumble-right-swipe":
            profile_id = args.profile_id or config.GEELARK_PROFILE_ID
            if not profile_id:
                raise AutomationError("Missing --profile-id or GEELARK_PROFILE_ID.")
            run_bumble_right_swipe_loop(
                config,
                profile_id=profile_id,
                max_count=args.max_count,
                prepare_adb=args.prepare_adb,
                use_api_shell=args.api_shell,
                start_tab=args.start_tab,
                output_root=Path(args.output_dir) if args.output_dir else None,
                skip_capture=args.skip_capture,
                view_profile_probability=args.view_profile_probability,
                view_profile_max_swipes=args.view_profile_max_swipes,
                until_daily_limit=args.until_daily_limit,
                popup_check_every_n_actions=args.popup_check_every_n_actions,
            )
            return

        if args.command == "bumble-capture-chat":
            profile_id = args.profile_id or config.GEELARK_PROFILE_ID
            phone_metadata: dict[str, Any] | None = None
            if not profile_id and (args.group_id or args.group_name):
                group = _find_group(config, group_id=args.group_id, group_name=args.group_name)
                group_id = str(group.get("id") or "")
                phones = [
                    phone
                    for phone in list_geelark_phones(config)
                    if str((phone.get("group") or {}).get("id") or "") == group_id
                ]
                if args.start_offset < 0:
                    raise AutomationError("--start-offset must be >= 0.")
                if args.start_offset >= len(phones):
                    raise AutomationError(
                        f"--start-offset {args.start_offset} is out of range for {len(phones)} phone(s)."
                    )
                phone_metadata = _phone_public_summary(phones[args.start_offset])
                profile_id = str(phone_metadata["id"])
            if not profile_id:
                raise AutomationError("Missing --profile-id or --group-id/--group-name.")
            if phone_metadata is None:
                phone_metadata = {"id": profile_id}
            summary = run_bumble_chat_capture(
                config,
                profile_id=profile_id,
                prepare_adb=args.prepare_adb,
                use_api_shell=args.api_shell,
                output_root=Path(args.output_dir) if args.output_dir else None,
                max_chats=args.max_chats,
                chat_scrolls=args.chat_scrolls,
                profile_scrolls=args.profile_scrolls,
                stop_phone_after_run=not args.keep_phone_running,
                economy_mode=args.economy_mode,
                phone_metadata=phone_metadata,
                save_screenshots=args.save_screenshots,
                list_only=args.list_only,
                send_ai_replies=args.send_ai_replies,
                capture_self_profile_for_ai=args.capture_self_profile_for_ai,
                target_location=args.target_location,
                heat_threshold=args.heat_threshold,
                force_reply=args.force_reply,
            )
            LOGGER.info("Chat capture summary: %s", json.dumps(summary, ensure_ascii=False))
            return

        if args.command == "bumble-group-right-swipe":
            if not args.group_id and not args.group_name:
                raise AutomationError("Missing --group-id or --group-name.")
            summary = run_bumble_group_right_swipe_loop(
                config,
                group_id=args.group_id,
                group_name=args.group_name,
                per_phone_min_count=args.per_phone_min_count,
                per_phone_max_count=args.per_phone_max_count,
                per_phone_until_daily_limit=args.per_phone_until_daily_limit,
                max_phones=args.max_phones,
                start_offset=args.start_offset,
                prepare_adb=args.prepare_adb,
                use_api_shell=args.api_shell,
                start_tab=args.start_tab,
                skip_capture=args.skip_capture,
                view_profile_probability=args.view_profile_probability,
                view_profile_max_swipes=args.view_profile_max_swipes,
                popup_check_every_n_actions=args.popup_check_every_n_actions,
                stop_phone_after_run=not args.keep_phone_running,
                prestart_phones=args.prestart_phones,
                economy_mode=args.economy_mode,
                capture_self_profile=args.capture_self_profile,
                output_root=Path(args.output_dir) if args.output_dir else None,
            )
            LOGGER.info("Group run summary: %s", json.dumps(summary, ensure_ascii=False))
            return

        if args.command == "bumble-group-reply-chats":
            if not args.group_id and not args.group_name:
                raise AutomationError("Missing --group-id or --group-name.")
            if args.monitor_loops < 0:
                raise AutomationError("--monitor-loops must be >= 0.")
            if args.monitor_sleep_min < 0 or args.monitor_sleep_max < 0:
                raise AutomationError("--monitor-sleep waits must be >= 0.")
            if args.monitor_sleep_min > args.monitor_sleep_max:
                raise AutomationError("--monitor-sleep-min cannot exceed --monitor-sleep-max.")

            monitor_root = Path(args.output_dir) if args.output_dir else None
            loop_summaries: list[dict[str, Any]] = []
            loop_idx = 0
            while args.monitor_loops == 0 or loop_idx < args.monitor_loops:
                loop_idx += 1
                loop_output_root = None
                if monitor_root is not None:
                    loop_output_root = monitor_root / f"loop_{loop_idx:03d}_{_timestamp_for_path()}"
                LOGGER.info(
                    "Starting group chat reply monitor loop %s/%s.",
                    loop_idx,
                    "infinite" if args.monitor_loops == 0 else args.monitor_loops,
                )
                summary = run_bumble_group_reply_chats(
                    config,
                    group_id=args.group_id,
                    group_name=args.group_name,
                    max_phones=args.max_phones,
                    start_offset=args.start_offset,
                    max_chats_per_phone=args.max_chats_per_phone,
                    chat_scrolls=args.chat_scrolls,
                    profile_scrolls=args.profile_scrolls,
                    prepare_adb=args.prepare_adb,
                    use_api_shell=args.api_shell,
                    stop_phone_after_run=not args.keep_phone_running,
                    economy_mode=not args.normal_waits,
                    save_screenshots=not args.no_screenshots,
                    capture_missing_self_profile=not args.no_capture_missing_self_profile,
                    target_location=args.target_location,
                    heat_threshold=args.heat_threshold,
                    force_reply=args.force_reply,
                    inter_phone_wait_min_seconds=args.inter_phone_wait_min,
                    inter_phone_wait_max_seconds=args.inter_phone_wait_max,
                    per_phone_timeout_seconds=args.per_phone_timeout_seconds,
                    output_root=loop_output_root if loop_output_root is not None else None,
                )
                loop_summaries.append(summary)
                LOGGER.info("Group chat reply summary: %s", json.dumps(summary, ensure_ascii=False))
                if args.monitor_loops != 0 and loop_idx >= args.monitor_loops:
                    break
                wait_s = random.uniform(args.monitor_sleep_min, args.monitor_sleep_max)
                LOGGER.info("Waiting %.2fs before the next group reply monitor loop.", wait_s)
                time.sleep(wait_s)
            if len(loop_summaries) > 1:
                LOGGER.info(
                    "Group chat reply monitor completed: %s",
                    json.dumps({"loops": len(loop_summaries), "summaries": loop_summaries}, ensure_ascii=False),
                )
            return

        runner = AutomationRunner(config)
        runner.run_forever()
    except DailyLimitExceeded as exc:
        LOGGER.warning("%s", exc)
    except AutomationError as exc:
        LOGGER.error("%s", exc)
    except KeyboardInterrupt:
        LOGGER.info("Interrupted by user.")


if __name__ == "__main__":
    main()
