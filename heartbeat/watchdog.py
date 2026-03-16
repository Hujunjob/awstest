from __future__ import annotations

import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from heartbeat.server.app import create_app
from heartbeat.server.bark import BarkClient
from heartbeat.server.config import load_config
from heartbeat.server.scanner import scan_once
from heartbeat.server.store import Store


def _scanner_loop(stop_event: threading.Event) -> None:
    config = load_config()
    store = Store(config.db_path)
    store.init_db()
    bark_client = BarkClient(config)

    while not stop_event.is_set():
        scan_once(
            store=store,
            config=config,
            bark_client=bark_client,
            now=datetime.now(timezone.utc),
        )
        stop_event.wait(config.scan_interval_seconds)


def main() -> None:
    config = load_config()
    app = create_app(config)
    if os.environ.get("HEARTBEAT_TEST_EXIT_AFTER_BOOTSTRAP") == "1":
        print("watchdog bootstrap ok")
        return

    stop_event = threading.Event()
    scanner_thread = threading.Thread(
        target=_scanner_loop,
        args=(stop_event,),
        name="heartbeat-scanner",
        daemon=True,
    )
    scanner_thread.start()

    try:
        app.run(
            host=os.environ.get("WATCHDOG_HOST", "0.0.0.0"),
            port=int(os.environ.get("WATCHDOG_PORT", "8000")),
        )
    finally:
        stop_event.set()
        scanner_thread.join(timeout=1)


if __name__ == "__main__":
    main()
