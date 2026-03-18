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


def _should_repeat_alert(row, repeat_seconds: int, now: datetime) -> bool:
    if row["last_alert_at"] is None:
        return True
    last_alert_at = _parse_timestamp(row["last_alert_at"])
    if last_alert_at is None:
        return True
    return (now - last_alert_at).total_seconds() >= repeat_seconds


def _format_wallet_alert_body(wallet_name: str, payload: dict | None) -> str:
    payload = payload or {}
    parts: list[str] = []
    wallet_note = str(payload.get("wallet_note", "") or "").strip()
    if wallet_note:
        parts.append(f"wallet_note={wallet_note}")
    for issue in payload.get("issues") or []:
        issue = issue or {}
        field = str(issue.get("field", "") or "")
        label = "Predict" if field == "predict_balance_usdt" else "PM"
        balance = float(issue.get("balance", 0.0) or 0.0)
        threshold = float(issue.get("threshold", 0.0) or 0.0)
        unit = str(issue.get("unit", "") or "").strip()
        parts.append(f"{label}={balance:.4f}<{threshold:.4f} {unit}".rstrip())
    errors = [str(item).strip() for item in (payload.get("errors") or []) if str(item).strip()]
    if errors:
        parts.append(f"errors={'; '.join(errors)}")
    return "; ".join(parts) if parts else f"wallet={wallet_name}"


def _format_wallet_alert_title(wallet_name: str, payload: dict | None) -> str:
    payload = payload or {}
    wallet_note = str(payload.get("wallet_note", "") or "").strip()
    title = f"Wallet balance low: {wallet_name}"
    if wallet_note:
        title = f"{title} ({wallet_note})"
    return title


def _scan_agents(store: Store, config: Config, now: datetime, bark_client=None) -> None:
    now_text = now.isoformat()
    for row in store.list_agents():
        seen_at = _parse_timestamp(row["last_seen_at"])
        if seen_at is None:
            continue

        age_seconds = (now - seen_at).total_seconds()
        next_status = _desired_status(age_seconds, config)

        if next_status == "alerting":
            if bark_client is not None and _should_repeat_alert(row, config.alert_repeat_seconds, now):
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


def _scan_wallets(store: Store, config: Config, now: datetime, bark_client=None) -> None:
    now_text = now.isoformat()
    for row in store.list_wallets():
        current_status = str(row["status"] or "unknown")
        if current_status != "alerting":
            store.update_wallet_status(row["wallet_name"], current_status)
            continue

        if bark_client is not None and _should_repeat_alert(row, config.wallet_alert_repeat_seconds, now):
            payload = store.decode_payload(row["last_payload"])
            bark_client.send_alert(
                agent_name=row["wallet_name"],
                body=_format_wallet_alert_body(row["wallet_name"], payload),
                title=_format_wallet_alert_title(row["wallet_name"], payload),
                group=f"balance/{row['wallet_name']}",
            )
            store.update_wallet_status(
                row["wallet_name"],
                current_status,
                last_alert_at=now_text,
            )
            continue

        if row["last_alert_at"] is None:
            store.update_wallet_status(
                row["wallet_name"],
                current_status,
                last_alert_at=now_text,
            )
        else:
            store.update_wallet_status(row["wallet_name"], current_status)


def scan_once(store: Store, config: Config, now: datetime, bark_client=None) -> None:
    _scan_agents(store=store, config=config, now=now, bark_client=bark_client)
    _scan_wallets(store=store, config=config, now=now, bark_client=bark_client)
