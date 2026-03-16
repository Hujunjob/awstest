from __future__ import annotations

import json
import sqlite3
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    agent_name TEXT PRIMARY KEY,
    last_seen_at TEXT,
    status TEXT NOT NULL DEFAULT 'unknown',
    last_alert_at TEXT,
    last_payload TEXT,
    silenced_until TEXT
)
"""


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def connect(self) -> sqlite3.Connection:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.execute(SCHEMA)
            conn.commit()

    def record_heartbeat(self, agent_name: str, payload: dict) -> None:
        payload_text = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        last_seen_at = payload.get("sent_at")
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO agents (
                    agent_name,
                    last_seen_at,
                    status,
                    last_alert_at,
                    last_payload,
                    silenced_until
                ) VALUES (?, ?, 'healthy', NULL, ?, NULL)
                ON CONFLICT(agent_name) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at,
                    status = 'healthy',
                    last_payload = excluded.last_payload
                """,
                (agent_name, last_seen_at, payload_text),
            )
            conn.commit()

    def get_agent(self, agent_name: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT agent_name, last_seen_at, status, last_alert_at, last_payload, silenced_until
                FROM agents
                WHERE agent_name = ?
                """,
                (agent_name,),
            ).fetchone()
        return row

    def list_agents(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT agent_name, last_seen_at, status, last_alert_at, last_payload, silenced_until
                FROM agents
                ORDER BY agent_name
                """
            ).fetchall()
        return rows

    def update_agent_status(
        self,
        agent_name: str,
        status: str,
        *,
        last_alert_at: str | None | object = None,
    ) -> None:
        if last_alert_at is _UNSET:
            query = "UPDATE agents SET status = ? WHERE agent_name = ?"
            params = (status, agent_name)
        else:
            query = "UPDATE agents SET status = ?, last_alert_at = ? WHERE agent_name = ?"
            params = (status, last_alert_at, agent_name)

        with self.connect() as conn:
            conn.execute(query, params)
            conn.commit()

    @staticmethod
    def decode_payload(payload_text: str | None) -> dict | None:
        if not payload_text:
            return None
        return json.loads(payload_text)


_UNSET = object()
