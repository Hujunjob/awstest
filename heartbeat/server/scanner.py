from __future__ import annotations

from datetime import datetime

from heartbeat.server.config import Config
from heartbeat.server.store import Store


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _desired_status(age_seconds: float, config: Config) -> str:
    if age_seconds > config.heartbeat_timeout_seconds:
        return "alerting"
    if age_seconds > config.heartbeat_late_seconds:
        return "late"
    return "healthy"


def _should_repeat_alert(row, config: Config, now: datetime) -> bool:
    if row["last_alert_at"] is None:
        return True
    last_alert_at = _parse_timestamp(row["last_alert_at"])
    if last_alert_at is None:
        return True
    return (now - last_alert_at).total_seconds() >= config.alert_repeat_seconds


def scan_once(store: Store, config: Config, now: datetime, bark_client=None) -> None:
    now_text = now.isoformat()
    for row in store.list_agents():
        seen_at = _parse_timestamp(row["last_seen_at"])
        if seen_at is None:
            continue

        age_seconds = (now - seen_at).total_seconds()
        next_status = _desired_status(age_seconds, config)

        if next_status == "alerting":
            if bark_client is not None and _should_repeat_alert(row, config, now):
                bark_client.send_alert(
                    agent_name=row["agent_name"],
                    body=f"Heartbeat missing for over {config.heartbeat_timeout_seconds} seconds.",
                )
                store.update_agent_status(
                    row["agent_name"],
                    next_status,
                    last_alert_at=now_text,
                )
            elif row["last_alert_at"] is None:
                store.update_agent_status(
                    row["agent_name"],
                    next_status,
                    last_alert_at=now_text,
                )
            else:
                store.update_agent_status(row["agent_name"], next_status)
            continue

        store.update_agent_status(row["agent_name"], next_status)
