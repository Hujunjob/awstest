#!/usr/bin/env python3
"""Network and auth probe for Predict / Polymarket / Polygon services.

This script is intended for Ubuntu or other Linux servers where you want to
validate that the external services required by the trading stack are reachable
and fast enough before starting live processes.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import ssl
import statistics
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = REPO_ROOT / "output" / "predict" / "predict_points.db"
DEFAULT_PREDICT_API_BASE_URL = "https://api.predict.fun"
DEFAULT_PREDICT_WS_URL = "wss://ws.predict.fun/ws"
DEFAULT_PREDICT_CHAIN_RPC_URL = "https://bsc-dataseed.bnbchain.org/"
DEFAULT_PM_CLOB_HOST = "https://clob.polymarket.com"
DEFAULT_PM_DATA_API_BASE_URL = "https://data-api.polymarket.com"
DEFAULT_PM_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
DEFAULT_PM_USER_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
DEFAULT_PM_RELAYER_URL = "https://relayer-v2.polymarket.com"
DEFAULT_POLYGON_RPC_URL = "https://polygon-rpc.com"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
PUBLIC_GROUPS = {"predict", "pm", "polygon"}
SENSITIVE_DETAIL_KEYS = {"api_key", "api_secret", "passphrase", "jwt", "private_key", "signature"}


@dataclass(frozen=True)
class ProbeSettings:
    env_file: Optional[Path]
    db_path: Path
    predict_market_id: int
    pm_token_id: str
    timeout_seconds: float
    ws_sample_seconds: float
    repeat: int
    only: Tuple[str, ...]
    skip_auth: bool
    json_output: bool
    slow_threshold_ms: float


@dataclass(frozen=True)
class ProbeSpec:
    name: str
    service: str
    group: str
    auth_required: bool = False


@dataclass(frozen=True)
class SampleTargets:
    predict_market_id: int = 0
    pm_token_id: str = ""


@dataclass
class ProbeResult:
    service: str
    target: str
    ok: bool
    latency_ms: Optional[float]
    phase: str
    error: str = ""
    http_status: Optional[int] = None
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


PROBE_SPECS: Tuple[ProbeSpec, ...] = (
    ProbeSpec("predict_rest_public", "Predict REST", "predict"),
    ProbeSpec("predict_auth_message", "Predict Auth Message", "predict", auth_required=True),
    ProbeSpec("predict_auth_jwt", "Predict JWT Auth", "predict", auth_required=True),
    ProbeSpec("predict_ws_public", "Predict WS", "predict"),
    ProbeSpec("predict_chain_rpc_http", "Predict Chain RPC", "predict"),
    ProbeSpec("pm_clob_public", "PM CLOB REST", "pm"),
    ProbeSpec("pm_data_public", "PM Data API", "pm"),
    ProbeSpec("pm_clob_auth", "PM CLOB Auth", "pm", auth_required=True),
    ProbeSpec("pm_ws_market", "PM Market WS", "pm"),
    ProbeSpec("pm_ws_user", "PM User WS", "pm", auth_required=True),
    ProbeSpec("pm_relayer_ready", "PM Relayer", "pm", auth_required=True),
    ProbeSpec("polygon_rpc_http", "Polygon RPC HTTP", "polygon"),
    ProbeSpec("polygon_rpc_ws", "Polygon RPC WS", "polygon"),
)


def parse_args(argv: Optional[Sequence[str]] = None) -> ProbeSettings:
    parser = argparse.ArgumentParser(description="Probe Predict / PM / Polygon connectivity and auth latency")
    parser.add_argument("--env-file", default="", help="Optional env file, e.g. envs/.env.wallet4")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="Optional mapping db used to resolve sample market/token")
    parser.add_argument("--predict-market-id", type=int, default=0, help="Optional sample Predict market id")
    parser.add_argument("--pm-token-id", default="", help="Optional sample PM token id")
    parser.add_argument("--timeout-seconds", type=float, default=8.0)
    parser.add_argument("--ws-sample-seconds", type=float, default=2.0)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--only", default="", help="Comma separated groups: predict,pm,polygon")
    parser.add_argument("--skip-auth", action="store_true", help="Skip auth-required probes")
    parser.add_argument("--json", dest="json_output", action="store_true", help="Print JSON output")
    parser.add_argument("--slow-threshold-ms", type=float, default=1500.0)
    args = parser.parse_args(argv)

    env_file_text = str(args.env_file).strip()
    only: List[str] = []
    for part in str(args.only).split(","):
        name = part.strip().lower()
        if name and name in PUBLIC_GROUPS and name not in only:
            only.append(name)

    return ProbeSettings(
        env_file=(Path(env_file_text).expanduser() if env_file_text else None),
        db_path=Path(args.db_path).expanduser(),
        predict_market_id=max(0, int(args.predict_market_id)),
        pm_token_id=str(args.pm_token_id).strip(),
        timeout_seconds=max(0.5, float(args.timeout_seconds)),
        ws_sample_seconds=max(0.1, float(args.ws_sample_seconds)),
        repeat=max(1, int(args.repeat)),
        only=tuple(only),
        skip_auth=bool(args.skip_auth),
        json_output=bool(args.json_output),
        slow_threshold_ms=max(0.0, float(args.slow_threshold_ms)),
    )


def _read_env_file_values(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        out[key] = value
    return out


def load_probe_env(env_file: Optional[Path]) -> Dict[str, str]:
    values = dict(os.environ)
    if env_file is None:
        return values
    file_values = _read_env_file_values(env_file)
    for key, value in file_values.items():
        values.setdefault(key, value)
    return values


def build_probe_specs(settings: ProbeSettings) -> List[ProbeSpec]:
    out: List[ProbeSpec] = []
    only = set(settings.only)
    for spec in PROBE_SPECS:
        if only and spec.group not in only:
            continue
        if settings.skip_auth and spec.auth_required:
            continue
        out.append(spec)
    return out


def resolve_sample_targets(db_path: Path) -> SampleTargets:
    if not db_path.exists():
        return SampleTargets()
    try:
        conn = sqlite3.connect(db_path)
    except Exception:
        return SampleTargets()
    try:
        row = conn.execute(
            """
            SELECT predict_market_id, COALESCE(predict_pm_outcome_pairs, '')
            FROM predict_pm_mapping
            WHERE map_status = 'MAPPED'
            ORDER BY predict_market_id ASC
            LIMIT 1
            """
        ).fetchone()
    except Exception:
        conn.close()
        return SampleTargets()
    conn.close()
    if not row:
        return SampleTargets()
    market_id = int(row[0] or 0)
    pairs_raw = str(row[1] or "")
    token_id = ""
    try:
        pairs = json.loads(pairs_raw) if pairs_raw else []
    except Exception:
        pairs = []
    if isinstance(pairs, list):
        for item in pairs:
            if not isinstance(item, dict):
                continue
            token = str(item.get("pm_token_id") or "").strip()
            if token:
                token_id = token
                break
    return SampleTargets(predict_market_id=market_id, pm_token_id=token_id)


def mask_secret(value: str) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 4:
        return "*" * len(text)
    return f"{text[:4]}...{text[-4:]}"


def _mask_detail_value(key: str, value: Any) -> Any:
    key_norm = str(key or "").lower()
    if any(piece in key_norm for piece in SENSITIVE_DETAIL_KEYS):
        return mask_secret(str(value))
    return value


def summarize_results(results: Iterable[ProbeResult], *, slow_threshold_ms: float = 1500.0) -> Dict[str, int]:
    rows = list(results)
    failed = sum(1 for row in rows if not row.ok)
    slow = sum(1 for row in rows if row.latency_ms is not None and row.latency_ms >= slow_threshold_ms)
    return {
        "total": len(rows),
        "ok": len(rows) - failed,
        "failed": failed,
        "slow": slow,
    }


def format_results_text(results: Iterable[ProbeResult], summary: Mapping[str, int]) -> str:
    lines = [
        "Network Probe Summary",
        f"total={int(summary.get('total', 0))} ok={int(summary.get('ok', 0))} failed={int(summary.get('failed', 0))} slow={int(summary.get('slow', 0))}",
        "",
    ]
    for row in results:
        latency = "-" if row.latency_ms is None else f"{row.latency_ms:.1f}ms"
        status = f" status={row.http_status}" if row.http_status is not None else ""
        error = f" error={row.error}" if row.error else ""
        lines.append(f"[{ 'OK' if row.ok else 'FAIL' }] {row.service} phase={row.phase} latency={latency}{status} target={row.target}{error}")
        if row.details:
            parts: List[str] = []
            for key in sorted(row.details.keys()):
                parts.append(f"{key}={_mask_detail_value(key, row.details[key])}")
            lines.append(f"  details: {' '.join(parts)}")
    return "\n".join(lines)


def _env_value(env: Mapping[str, str], *names: str, default: str = "") -> str:
    for name in names:
        value = str(env.get(name, "") or "").strip()
        if value:
            return value
    return default


def _predict_api_base_url(env: Mapping[str, str]) -> str:
    return _env_value(env, "PREDICT_API_BASE_URL", "PREDICT_BASE_URL", default=DEFAULT_PREDICT_API_BASE_URL).rstrip("/")


def _predict_ws_url(env: Mapping[str, str]) -> str:
    return _env_value(env, "PREDICT_WS_URL", default=DEFAULT_PREDICT_WS_URL)


def _predict_chain_rpc_url(env: Mapping[str, str]) -> str:
    return _env_value(env, "PREDICT_CHAIN_RPC_URL", "PREDICT_RPC_URL", default=DEFAULT_PREDICT_CHAIN_RPC_URL)


def _pm_clob_host(env: Mapping[str, str]) -> str:
    return _env_value(env, "PM_CLOB_HOST", default=DEFAULT_PM_CLOB_HOST).rstrip("/")


def _pm_data_api_base_url(env: Mapping[str, str]) -> str:
    return _env_value(env, "PM_DATA_API_BASE_URL", default=DEFAULT_PM_DATA_API_BASE_URL).rstrip("/")


def _pm_ws_url(env: Mapping[str, str]) -> str:
    return _env_value(env, "PM_WS_URL", default=DEFAULT_PM_WS_URL)


def _pm_user_ws_url(env: Mapping[str, str]) -> str:
    return _env_value(env, "PM_USER_WS_URL", default=DEFAULT_PM_USER_WS_URL)


def _polygon_rpc_http_url(env: Mapping[str, str]) -> str:
    value = _env_value(env, "PM_CHAIN_RPC_URL", "POLYGON_RPC_URL", default=DEFAULT_POLYGON_RPC_URL)
    if value.lower().startswith(("ws://", "wss://")):
        return ""
    return value


def _polygon_rpc_ws_url(env: Mapping[str, str]) -> str:
    value = _env_value(env, "PM_CHAIN_WS_URL", "POLYGON_WS_URL")
    if value.lower().startswith(("ws://", "wss://")):
        return value
    for name in ("PM_CHAIN_RPC_URL", "POLYGON_RPC_URL"):
        candidate = str(env.get(name, "") or "").strip()
        if candidate.lower().startswith(("ws://", "wss://")):
            return candidate
    return ""


@contextmanager
def _temporary_environ(values: Mapping[str, str]):
    sentinel = object()
    previous: Dict[str, object] = {}
    for key, value in values.items():
        previous[key] = os.environ.get(key, sentinel)
        os.environ[key] = value
    try:
        yield
    finally:
        for key, old_value in previous.items():
            if old_value is sentinel:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(old_value)


def _http_request(
    *,
    method: str,
    url: str,
    headers: Optional[Mapping[str, str]] = None,
    body: Optional[Dict[str, Any]] = None,
    timeout_seconds: float,
) -> Tuple[Optional[int], str, Optional[Any], float]:
    payload: Optional[bytes] = None
    req_headers = {str(k): str(v) for k, v in (headers or {}).items()}
    if body is not None:
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")
    request = Request(url=url, method=method.upper(), data=payload, headers=req_headers)
    started = time.monotonic()
    try:
        with urlopen(request, timeout=timeout_seconds, context=ssl.create_default_context()) as resp:
            raw = resp.read(65536)
            text = raw.decode("utf-8", errors="replace")
            status = int(resp.getcode())
    except HTTPError as exc:
        elapsed_ms = (time.monotonic() - started) * 1000.0
        raw = exc.read(65536)
        text = raw.decode("utf-8", errors="replace")
        payload_out: Optional[Any] = None
        try:
            payload_out = json.loads(text) if text else None
        except Exception:
            payload_out = None
        return int(exc.code), text, payload_out, elapsed_ms
    except URLError:
        raise
    elapsed_ms = (time.monotonic() - started) * 1000.0
    payload_out = None
    try:
        payload_out = json.loads(text) if text else None
    except Exception:
        payload_out = None
    return status, text, payload_out, elapsed_ms


def _require(value: str, *, what: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise RuntimeError(f"missing {what}")
    return text


def _load_websocket_module():
    try:
        import websocket  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on local env
        raise RuntimeError("missing dependency: pip install websocket-client") from exc
    return websocket


def _ws_probe(
    *,
    url: str,
    timeout_seconds: float,
    wait_seconds: float,
    send_payload: Optional[Dict[str, Any]] = None,
) -> Tuple[float, str, str]:
    websocket = _load_websocket_module()
    started = time.monotonic()
    ws = websocket.create_connection(url, timeout=timeout_seconds)
    phase = "ws_open"
    message_text = ""
    try:
        open_elapsed = (time.monotonic() - started) * 1000.0
        if send_payload is not None:
            ws.send(json.dumps(send_payload, separators=(",", ":")))
            phase = "ws_send"
        if wait_seconds > 0:
            ws.settimeout(wait_seconds)
            try:
                message = ws.recv()
                if isinstance(message, bytes):
                    message_text = message.decode("utf-8", errors="replace")
                else:
                    message_text = str(message)
                phase = "ws_message"
            except websocket.WebSocketTimeoutException:
                pass
        return open_elapsed, phase, message_text
    finally:
        try:
            ws.close()
        except Exception:
            pass


def _rpc_http_probe(url: str, *, timeout_seconds: float) -> Tuple[Optional[int], float, Optional[Any]]:
    status, _text, payload, elapsed_ms = _http_request(
        method="POST",
        url=url,
        headers={"Accept": "application/json"},
        body={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []},
        timeout_seconds=timeout_seconds,
    )
    return status, elapsed_ms, payload


def _rpc_ws_probe(url: str, *, timeout_seconds: float, wait_seconds: float) -> Tuple[float, str]:
    latency_ms, phase, message_text = _ws_probe(
        url=url,
        timeout_seconds=timeout_seconds,
        wait_seconds=wait_seconds,
        send_payload={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []},
    )
    return latency_ms, message_text or phase


def _mean(values: Iterable[Optional[float]]) -> Optional[float]:
    rows = [float(v) for v in values if v is not None]
    if not rows:
        return None
    return statistics.mean(rows)


def _aggregate_probe_runs(service: str, target: str, rows: List[ProbeResult]) -> ProbeResult:
    ok = all(row.ok for row in rows)
    error_parts = [row.error for row in rows if row.error]
    last_status = next((row.http_status for row in reversed(rows) if row.http_status is not None), None)
    last_phase = rows[-1].phase if rows else "unknown"
    details: Dict[str, Any] = {}
    sample_latencies = [round(row.latency_ms, 3) for row in rows if row.latency_ms is not None]
    if len(rows) > 1:
        details["attempts"] = len(rows)
        details["samples_ms"] = sample_latencies
    for row in rows:
        details.update(row.details)
    return ProbeResult(
        service=service,
        target=target,
        ok=ok,
        latency_ms=_mean(row.latency_ms for row in rows),
        phase=last_phase,
        error="; ".join(dict.fromkeys(error_parts)),
        http_status=last_status,
        details=details,
    )


def _probe_predict_rest_public(env: Dict[str, str], settings: ProbeSettings, targets: SampleTargets) -> ProbeResult:
    market_id = targets.predict_market_id
    if market_id <= 0:
        raise RuntimeError("missing sample predict market id (pass --predict-market-id or provide mapping db)")
    url = f"{_predict_api_base_url(env)}/v1/markets/{market_id}/orderbook"
    status, _text, _payload, elapsed_ms = _http_request(method="GET", url=url, headers={"Accept": "application/json"}, timeout_seconds=settings.timeout_seconds)
    return ProbeResult(service="Predict REST", target=url, ok=bool(status and 200 <= status < 300), latency_ms=elapsed_ms, phase="http", http_status=status, error=("" if status and 200 <= status < 300 else f"http_status={status}"), details={"market_id": market_id})


def _probe_predict_auth_message(env: Dict[str, str], settings: ProbeSettings, _targets: SampleTargets) -> ProbeResult:
    api_key = _require(env.get("PREDICT_API_KEY", ""), what="PREDICT_API_KEY")
    url = f"{_predict_api_base_url(env)}/v1/auth/message"
    status, _text, payload, elapsed_ms = _http_request(method="GET", url=url, headers={"Accept": "application/json", "x-api-key": api_key}, timeout_seconds=settings.timeout_seconds)
    ok = bool(status and 200 <= status < 300 and isinstance(payload, dict))
    return ProbeResult(service="Predict Auth Message", target=url, ok=ok, latency_ms=elapsed_ms, phase="auth_message", http_status=status, error=("" if ok else f"http_status={status}"), details={"api_key": api_key})


def _probe_predict_auth_jwt(env: Dict[str, str], _settings: ProbeSettings, _targets: SampleTargets) -> ProbeResult:
    from predict_pm_consumer import predict_live_executor_py as predict_exec

    started = time.monotonic()
    with _temporary_environ(env):
        wallet = predict_exec._wallet_from_env()
        signer = predict_exec._resolve_order_signer_address(wallet)
        auth = predict_exec._ensure_jwt(order_signer_address=signer, wallet=wallet, order_builder=None)
    elapsed_ms = (time.monotonic() - started) * 1000.0
    if auth.jwt_token:
        env["PREDICT_JWT"] = auth.jwt_token
    return ProbeResult(service="Predict JWT Auth", target=f"{auth.base_url}/v1/auth", ok=bool(auth.jwt_token), latency_ms=elapsed_ms, phase="auth_jwt", details={"signer": signer, "jwt": auth.jwt_token})


def _probe_predict_ws_public(env: Dict[str, str], settings: ProbeSettings, targets: SampleTargets) -> ProbeResult:
    url = _predict_ws_url(env)
    payload = None
    details: Dict[str, Any] = {}
    if targets.predict_market_id > 0:
        payload = {"method": "subscribe", "requestId": 1, "params": [f"predictOrderbook/{targets.predict_market_id}"]}
        details["market_id"] = targets.predict_market_id
    latency_ms, phase, message_text = _ws_probe(url=url, timeout_seconds=settings.timeout_seconds, wait_seconds=settings.ws_sample_seconds, send_payload=payload)
    if message_text:
        details["message_preview"] = message_text[:120]
    return ProbeResult(service="Predict WS", target=url, ok=True, latency_ms=latency_ms, phase=phase, details=details)


def _probe_predict_chain_rpc_http(env: Dict[str, str], settings: ProbeSettings, _targets: SampleTargets) -> ProbeResult:
    url = _predict_chain_rpc_url(env)
    status, elapsed_ms, payload = _rpc_http_probe(url, timeout_seconds=settings.timeout_seconds)
    ok = bool(status and 200 <= status < 300 and isinstance(payload, dict) and payload.get("result"))
    return ProbeResult(service="Predict Chain RPC", target=url, ok=ok, latency_ms=elapsed_ms, phase="rpc_http", http_status=status, error=("" if ok else f"http_status={status}"))


def _probe_pm_clob_public(env: Dict[str, str], settings: ProbeSettings, targets: SampleTargets) -> ProbeResult:
    token_id = _require(targets.pm_token_id, what="sample pm token id (pass --pm-token-id or provide mapping db)")
    url = f"{_pm_clob_host(env)}/book?{urlencode({'token_id': token_id})}"
    status, _text, _payload, elapsed_ms = _http_request(method="GET", url=url, headers={"Accept": "application/json"}, timeout_seconds=settings.timeout_seconds)
    ok = bool(status and 200 <= status < 300)
    return ProbeResult(service="PM CLOB REST", target=url, ok=ok, latency_ms=elapsed_ms, phase="http", http_status=status, error=("" if ok else f"http_status={status}"), details={"token_id": token_id})


def _probe_pm_data_public(env: Dict[str, str], settings: ProbeSettings, _targets: SampleTargets) -> ProbeResult:
    user = _env_value(env, "PM_FUNDER", "PM_ADDRESS", default=ZERO_ADDRESS)
    url = f"{_pm_data_api_base_url(env)}/positions?{urlencode({'user': user, 'sizeThreshold': '0', 'limit': '1'})}"
    status, _text, payload, elapsed_ms = _http_request(method="GET", url=url, headers={"Accept": "application/json"}, timeout_seconds=settings.timeout_seconds)
    ok = bool(status and 200 <= status < 300 and isinstance(payload, list))
    return ProbeResult(service="PM Data API", target=url, ok=ok, latency_ms=elapsed_ms, phase="http", http_status=status, error=("" if ok else f"http_status={status}"), details={"user": user})


def _resolve_pm_user_ws_auth(env: Dict[str, str]) -> Dict[str, str]:
    api_key = _env_value(env, "PM_API_KEY", "PM_CLOB_API_KEY", "CLOB_API_KEY")
    api_secret = _env_value(env, "PM_API_SECRET", "PM_CLOB_SECRET", "CLOB_SECRET")
    api_passphrase = _env_value(env, "PM_API_PASSPHRASE", "PM_CLOB_PASSPHRASE", "CLOB_PASS_PHRASE")
    if api_key and api_secret and api_passphrase:
        return {"apiKey": api_key, "secret": api_secret, "passphrase": api_passphrase}

    from predict_pm_consumer import polymarket_live_executor_py as pm_exec

    private_key = _require(env.get("PM_PRIVATE_KEY", ""), what="PM_PRIVATE_KEY")
    chain_id = int(_env_value(env, "PM_CHAIN_ID", default="137") or "137")
    host = _pm_clob_host(env)
    signature_type = int(_env_value(env, "PM_SIGNATURE_TYPE", default="0") or "0")
    funder = _env_value(env, "PM_FUNDER") or None

    with _temporary_environ(env):
        creds = pm_exec._api_creds_from_env() or pm_exec._load_cached_api_creds(private_key)
        if creds is None:
            bootstrap = pm_exec.ClobClient(host, key=private_key, chain_id=chain_id, signature_type=signature_type, funder=funder)
            creds = bootstrap.create_or_derive_api_creds()
            if creds is not None:
                pm_exec._save_cached_api_creds(private_key, creds)
        if creds is None:
            raise RuntimeError("could not derive PM websocket api creds")
        api_key = str(getattr(creds, "api_key", "") or "")
        api_secret = str(getattr(creds, "api_secret", "") or "")
        api_passphrase = str(getattr(creds, "api_passphrase", "") or "")
    if not (api_key and api_secret and api_passphrase):
        raise RuntimeError("missing derived PM websocket api creds")
    env.setdefault("PM_API_KEY", api_key)
    env.setdefault("PM_API_SECRET", api_secret)
    env.setdefault("PM_API_PASSPHRASE", api_passphrase)
    return {"apiKey": api_key, "secret": api_secret, "passphrase": api_passphrase}


def _probe_pm_clob_auth(env: Dict[str, str], _settings: ProbeSettings, _targets: SampleTargets) -> ProbeResult:
    from predict_pm_consumer import polymarket_live_executor_py as pm_exec

    private_key = _require(env.get("PM_PRIVATE_KEY", ""), what="PM_PRIVATE_KEY")
    chain_id = int(_env_value(env, "PM_CHAIN_ID", default="137") or "137")
    started = time.monotonic()
    with _temporary_environ(env):
        result = pm_exec._action_auth({}, chain_id=chain_id, private_key=private_key)
    elapsed_ms = (time.monotonic() - started) * 1000.0
    ok = bool(result.get("success"))
    cache_path = ((result.get("response") or {}) if isinstance(result, dict) else {}).get("cache_path", "")
    return ProbeResult(service="PM CLOB Auth", target=_pm_clob_host(env), ok=ok, latency_ms=elapsed_ms, phase="auth", error=("" if ok else str(result.get('error') or 'pm auth failed')), details={"cache_path": cache_path})


def _probe_pm_ws_market(env: Dict[str, str], settings: ProbeSettings, targets: SampleTargets) -> ProbeResult:
    token_id = _require(targets.pm_token_id, what="sample pm token id (pass --pm-token-id or provide mapping db)")
    url = _pm_ws_url(env)
    latency_ms, phase, message_text = _ws_probe(url=url, timeout_seconds=settings.timeout_seconds, wait_seconds=settings.ws_sample_seconds, send_payload={"assets_ids": [token_id], "type": "market"})
    details: Dict[str, Any] = {"token_id": token_id}
    if message_text:
        details["message_preview"] = message_text[:120]
    return ProbeResult(service="PM Market WS", target=url, ok=True, latency_ms=latency_ms, phase=phase, details=details)


def _probe_pm_ws_user(env: Dict[str, str], settings: ProbeSettings, _targets: SampleTargets) -> ProbeResult:
    url = _pm_user_ws_url(env)
    auth = _resolve_pm_user_ws_auth(env)
    latency_ms, phase, message_text = _ws_probe(url=url, timeout_seconds=settings.timeout_seconds, wait_seconds=settings.ws_sample_seconds, send_payload={"auth": auth, "type": "user"})
    details: Dict[str, Any] = {"api_key": auth.get("apiKey", "")}
    if message_text:
        details["message_preview"] = message_text[:120]
    return ProbeResult(service="PM User WS", target=url, ok=True, latency_ms=latency_ms, phase=phase, details=details)


def _probe_pm_relayer_ready(env: Dict[str, str], _settings: ProbeSettings, _targets: SampleTargets) -> ProbeResult:
    from predict_pm_consumer import polymarket_live_executor_py as pm_exec

    private_key = _require(env.get("PM_PRIVATE_KEY", ""), what="PM_PRIVATE_KEY")
    chain_id = int(_env_value(env, "PM_CHAIN_ID", default="137") or "137")
    started = time.monotonic()
    with _temporary_environ(env):
        result = pm_exec._action_safe_address(chain_id=chain_id, private_key=private_key)
    elapsed_ms = (time.monotonic() - started) * 1000.0
    ok = bool(result.get("success"))
    response = result.get("response") if isinstance(result, dict) else {}
    return ProbeResult(service="PM Relayer", target=_env_value(env, "PM_RELAYER_URL", default=DEFAULT_PM_RELAYER_URL), ok=ok, latency_ms=elapsed_ms, phase="relayer", error=("" if ok else str(result.get('error') or 'pm relayer failed')), details={"safe": str((response or {}).get('safe', '')), "deployed": bool((response or {}).get('deployed', False))})


def _probe_polygon_rpc_http(env: Dict[str, str], settings: ProbeSettings, _targets: SampleTargets) -> ProbeResult:
    url = _require(_polygon_rpc_http_url(env), what="polygon rpc http url")
    status, elapsed_ms, payload = _rpc_http_probe(url, timeout_seconds=settings.timeout_seconds)
    ok = bool(status and 200 <= status < 300 and isinstance(payload, dict) and payload.get("result"))
    return ProbeResult(service="Polygon RPC HTTP", target=url, ok=ok, latency_ms=elapsed_ms, phase="rpc_http", http_status=status, error=("" if ok else f"http_status={status}"))


def _probe_polygon_rpc_ws(env: Dict[str, str], settings: ProbeSettings, _targets: SampleTargets) -> ProbeResult:
    url = _require(_polygon_rpc_ws_url(env), what="polygon rpc ws url")
    latency_ms, response_preview = _rpc_ws_probe(url, timeout_seconds=settings.timeout_seconds, wait_seconds=settings.ws_sample_seconds)
    return ProbeResult(service="Polygon RPC WS", target=url, ok=True, latency_ms=latency_ms, phase="rpc_ws", details={"message_preview": response_preview[:120]})


PROBE_RUNNERS = {
    "predict_rest_public": _probe_predict_rest_public,
    "predict_auth_message": _probe_predict_auth_message,
    "predict_auth_jwt": _probe_predict_auth_jwt,
    "predict_ws_public": _probe_predict_ws_public,
    "predict_chain_rpc_http": _probe_predict_chain_rpc_http,
    "pm_clob_public": _probe_pm_clob_public,
    "pm_data_public": _probe_pm_data_public,
    "pm_clob_auth": _probe_pm_clob_auth,
    "pm_ws_market": _probe_pm_ws_market,
    "pm_ws_user": _probe_pm_ws_user,
    "pm_relayer_ready": _probe_pm_relayer_ready,
    "polygon_rpc_http": _probe_polygon_rpc_http,
    "polygon_rpc_ws": _probe_polygon_rpc_ws,
}


def run_selected_probes(settings: ProbeSettings, env: Dict[str, str]) -> List[ProbeResult]:
    sample_targets = resolve_sample_targets(settings.db_path)
    if settings.predict_market_id > 0:
        sample_targets = SampleTargets(predict_market_id=settings.predict_market_id, pm_token_id=sample_targets.pm_token_id)
    if settings.pm_token_id:
        sample_targets = SampleTargets(predict_market_id=sample_targets.predict_market_id, pm_token_id=settings.pm_token_id)

    results: List[ProbeResult] = []
    for spec in build_probe_specs(settings):
        target_for_result = spec.service
        attempts: List[ProbeResult] = []
        for _ in range(settings.repeat):
            try:
                row = PROBE_RUNNERS[spec.name](env, settings, sample_targets)
            except Exception as exc:  # noqa: BLE001
                row = ProbeResult(service=spec.service, target=target_for_result, ok=False, latency_ms=None, phase="error", error=str(exc))
            if row.target:
                target_for_result = row.target
            attempts.append(row)
        results.append(_aggregate_probe_runs(spec.service, target_for_result, attempts))
    return results


def main(argv: Optional[Sequence[str]] = None) -> int:
    settings = parse_args(argv)
    if settings.env_file is not None and (not settings.env_file.exists()):
        raise SystemExit(f"env file not found: {settings.env_file}")
    env = load_probe_env(settings.env_file)
    results = run_selected_probes(settings, env)
    summary = summarize_results(results, slow_threshold_ms=settings.slow_threshold_ms)
    if settings.json_output:
        print(json.dumps({"summary": summary, "results": [row.to_dict() for row in results]}, ensure_ascii=False, indent=2))
    else:
        print(format_results_text(results, summary))
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
