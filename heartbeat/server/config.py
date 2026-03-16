from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


@dataclass(frozen=True)
class Config:
    heartbeat_token: str
    db_path: str
    bark_device_key: str
    bark_base_url: str = "https://api.day.app"
    bark_sound: str = "alarm"
    heartbeat_late_seconds: int = 60
    heartbeat_timeout_seconds: int = 300
    alert_repeat_seconds: int = 30
    scan_interval_seconds: int = 60


def load_env_file(path: str | None = None) -> None:
    env_path = Path(path or os.environ.get("HEARTBEAT_ENV_FILE") or DEFAULT_ENV_FILE)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def load_config() -> Config:
    load_env_file()
    return Config(
        heartbeat_token=require_env("HEARTBEAT_TOKEN"),
        db_path=os.environ.get("HEARTBEAT_DB_PATH", "heartbeat/watchdog.db"),
        bark_device_key=require_env("BARK_DEVICE_KEY"),
        bark_base_url=os.environ.get("BARK_BASE_URL", "https://api.day.app").rstrip("/"),
        bark_sound=os.environ.get("BARK_SOUND", "alarm"),
        heartbeat_late_seconds=int(os.environ.get("HEARTBEAT_LATE_SECONDS", "60")),
        heartbeat_timeout_seconds=int(os.environ.get("HEARTBEAT_TIMEOUT_SECONDS", "300")),
        alert_repeat_seconds=int(os.environ.get("ALERT_REPEAT_SECONDS", "30")),
        scan_interval_seconds=int(os.environ.get("SCAN_INTERVAL_SECONDS", "60")),
    )
