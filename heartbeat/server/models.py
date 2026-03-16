from dataclasses import dataclass


@dataclass(frozen=True)
class AgentState:
    agent_name: str
    last_seen_at: str | None
    status: str
    last_alert_at: str | None
    last_payload: dict | None
