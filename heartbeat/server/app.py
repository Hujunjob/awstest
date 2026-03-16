from __future__ import annotations

from flask import Flask, abort, jsonify, request

from heartbeat.server.config import Config, load_config
from heartbeat.server.store import Store


def _is_authorized(auth_header: str | None, token: str) -> bool:
    return auth_header == f"Bearer {token}"


def create_app(config: Config | None = None) -> Flask:
    app = Flask(__name__)
    app.config["HEARTBEAT_CONFIG"] = config or load_config()
    app.config["STORE"] = Store(app.config["HEARTBEAT_CONFIG"].db_path)
    app.config["STORE"].init_db()

    def require_auth():
        token = app.config["HEARTBEAT_CONFIG"].heartbeat_token
        if not _is_authorized(request.headers.get("Authorization"), token):
            return jsonify({"error": "unauthorized"}), 401
        return None

    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok"})

    @app.post("/api/v1/agents/<agent_name>/heartbeat")
    def heartbeat(agent_name: str):
        unauthorized = require_auth()
        if unauthorized is not None:
            return unauthorized

        payload = request.get_json(silent=True) or {}
        app.config["STORE"].record_heartbeat(agent_name, payload)
        return jsonify({"agent_name": agent_name, "status": "accepted"}), 202

    @app.get("/api/v1/agents/<agent_name>")
    def inspect_agent(agent_name: str):
        unauthorized = require_auth()
        if unauthorized is not None:
            return unauthorized

        row = app.config["STORE"].get_agent(agent_name)
        if row is None:
            abort(404)

        return jsonify(
            {
                "agent_name": row["agent_name"],
                "last_seen_at": row["last_seen_at"],
                "status": row["status"],
                "last_alert_at": row["last_alert_at"],
                "last_payload": app.config["STORE"].decode_payload(row["last_payload"]),
            }
        )

    return app


if __name__ == "__main__":
    create_app().run(host="0.0.0.0", port=8000)
