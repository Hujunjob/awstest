from __future__ import annotations

import json
import sqlite3
from pathlib import Path


AGENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    agent_name TEXT PRIMARY KEY,
    last_seen_at TEXT,
    status TEXT NOT NULL DEFAULT 'unknown',
    last_alert_at TEXT,
    last_payload TEXT,
    silenced_until TEXT
)
"""

WALLETS_SCHEMA = """
CREATE TABLE IF NOT EXISTS wallets (
    wallet_name TEXT PRIMARY KEY,
    client_name TEXT,
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
            conn.execute(AGENTS_SCHEMA)
            conn.execute(WALLETS_SCHEMA)
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

    def record_wallet_status(self, wallet_name: str, payload: dict) -> None:
        payload_text = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        last_seen_at = payload.get("checked_at")
        client_name = payload.get("client_name")
        status = str(payload.get("status", "unknown") or "unknown")
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO wallets (
                    wallet_name,
                    client_name,
                    last_seen_at,
                    status,
                    last_alert_at,
                    last_payload,
                    silenced_until
                ) VALUES (?, ?, ?, ?, NULL, ?, NULL)
                ON CONFLICT(wallet_name) DO UPDATE SET
                    client_name = excluded.client_name,
                    last_seen_at = excluded.last_seen_at,
                    status = excluded.status,
                    last_payload = excluded.last_payload
                """,
                (wallet_name, client_name, last_seen_at, status, payload_text),
            )
            conn.commit()

    def get_wallet(self, wallet_name: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT wallet_name, client_name, last_seen_at, status, last_alert_at, last_payload, silenced_until
                FROM wallets
                WHERE wallet_name = ?
                """,
                (wallet_name,),
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

    def list_wallets(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT wallet_name, client_name, last_seen_at, status, last_alert_at, last_payload, silenced_until
                FROM wallets
                ORDER BY wallet_name
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

    def update_wallet_status(
        self,
        wallet_name: str,
        status: str,
        *,
        last_alert_at: str | None | object = None,
    ) -> None:
        if last_alert_at is _UNSET:
            query = "UPDATE wallets SET status = ? WHERE wallet_name = ?"
            params = (status, wallet_name)
        else:
            query = "UPDATE wallets SET status = ?, last_alert_at = ? WHERE wallet_name = ?"
            params = (status, last_alert_at, wallet_name)

        with self.connect() as conn:
            conn.execute(query, params)
            conn.commit()

    @staticmethod
    def decode_payload(payload_text: str | None) -> dict | None:
        if not payload_text:
            return None
        return json.loads(payload_text)


_UNSET = object()
