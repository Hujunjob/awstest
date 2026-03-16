from __future__ import annotations

import requests

from heartbeat.server.config import Config


class BarkClient:
    def __init__(self, config: Config, session=None):
        self.config = config
        self.session = session or requests.Session()

    def send_alert(self, *, agent_name: str, body: str) -> None:
        response = self.session.post(
            f"{self.config.bark_base_url}/push",
            json={
                "device_key": self.config.bark_device_key,
                "title": f"Heartbeat alert: {agent_name}",
                "body": body,
                "level": "critical",
                "call": "1",
                "sound": self.config.bark_sound,
                "group": f"heartbeat/{agent_name}",
            },
            timeout=10,
        )
        response.raise_for_status()
