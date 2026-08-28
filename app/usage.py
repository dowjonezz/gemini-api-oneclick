"""Gemini account usage/limit diagnostics for the OneClick worker."""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from gemini_webapi.constants import Endpoint, Headers, MODEL_HEADER_KEY
from gemini_webapi.utils import extract_json_from_response, get_nested_value

USAGE_RPC_ID = "jSf9Qc"
DEFAULT_CACHE_TTL = 60.0

_TIER_LABELS = {1: "FREE", 2: "PRO", 3: "ULTRA", 4: "PLUS", 6: "ULTRA"}
_METRIC_WINDOWS = {1: ("current_5h", "5h"), 2: ("weekly", "weekly")}
_UPSTREAM_BATCH_MODEL_HEADER = [
    1, None, None, None, None, None, None, None,
    [4, 5, 6, 8], None, None, None, None, None, 1, 1,
]
_BROWSER_USAGE_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
    "Sec-CH-UA": '"Chromium";v="152", "Not?A_Brand";v="24", "Google Chrome";v="152"',
    "Sec-CH-UA-Arch": '"x86"',
    "Sec-CH-UA-Bitness": '"64"',
    "Sec-CH-UA-Form-Factors": '"Desktop"',
    "Sec-CH-UA-Full-Version": '"152.0.7977.64"',
    "Sec-CH-UA-Full-Version-List": '"Chromium";v="152.0.7977.64", "Not?A_Brand";v="24.0.0.0", "Google Chrome";v="152.0.7977.64"',
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Model": '""',
    "Sec-CH-UA-Platform": '"Windows"',
    "Sec-CH-UA-Platform-Version": '"19.0.0"',
    "Sec-CH-UA-WoW64": "?0",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "X-Browser-Channel": "stable",
    "X-Browser-Copyright": "Copyright 2026 Google LLC. All Rights Reserved.",
    "X-Browser-Validation": "DD7V8Qhc96Al9nfPAmKmyHrwyTQ=",
    "X-Browser-Year": "2026",
    "X-Client-Data": "CKmdygEIlqHLAQiFoM0BCIjTlDAI7d+UMBin3pQw",
}
_cache: dict[int, tuple[float, dict[str, Any]]] = {}
_locks: dict[int, asyncio.Lock] = {}
_usage_session_ids: dict[int, str] = {}


def _utc_iso(timestamp: Any) -> str | None:
    if not isinstance(timestamp, (int, float)) or timestamp <= 0:
        return None
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def parse_usage_body(part_body: Any) -> dict[str, Any]:
    if not isinstance(part_body, list):
        return {}
    tier_id = get_nested_value(part_body, [0])
    usage_items = get_nested_value(part_body, [1], [])
    use_overage_ai_credits = get_nested_value(part_body, [2])
    result: dict[str, Any] = {
        "available": True,
        "tier": {"id": tier_id, "label": _TIER_LABELS.get(tier_id) or "UNKNOWN"},
        "use_overage_ai_credits": use_overage_ai_credits,
        "current_5h": None,
        "weekly": None,
    }
    if not isinstance(usage_items, list):
        return result
    for item in usage_items:
        remaining = get_nested_value(item, [0])
        usage_level = get_nested_value(item, [1])
        metric_type = get_nested_value(item, [2])
        reset_ts = get_nested_value(item, [3, 0, 0])
        if metric_type == 3:
            result["ai_credits_remaining"] = remaining
            continue
        metric_label, window = _METRIC_WINDOWS.get(metric_type, (f"type_{metric_type}", "unknown"))
        usage_percentage = None
        if isinstance(usage_level, (int, float)):
            usage_percentage = max(0, min(100, round(usage_level * 100)))
        result[metric_label] = {
            "type": metric_type,
            "window": window,
            "remaining_credits": remaining,
            "usage_level": usage_level,
            "usage_percentage": usage_percentage,
            "reset_at": _utc_iso(reset_ts),
        }
    return result


def _extract_usage_body(response_text: str) -> Any:
    try:
        parts = extract_json_from_response(response_text)
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    for part in parts:
        if get_nested_value(part, [1]) != USAGE_RPC_ID:
            continue
        body_text = get_nested_value(part, [2])
        if not body_text:
            continue
        try:
            return json.loads(body_text)
        except (json.JSONDecodeError, TypeError):
            continue
    return None


def _batch_headers_for_usage(client: Any) -> dict[str, str]:
    key = id(client)
    batch_session_id = getattr(client, "_sessionid", None)
    if not batch_session_id:
        batch_session_id = _usage_session_ids.setdefault(key, str(uuid.uuid4()).upper())

    model_header = list(_UPSTREAM_BATCH_MODEL_HEADER)
    model_header.append(batch_session_id)
    batch_headers = {
        MODEL_HEADER_KEY: json.dumps(model_header, separators=(",", ":")),
        "x-goog-ext-73010989-jspb": "[0]",
    }
    return {
        **Headers.GEMINI.value,
        **_BROWSER_USAGE_HEADERS,
        **batch_headers,
        **Headers.SAME_DOMAIN.value,
    }


async def _request_usage_response(client: Any):
    session = getattr(client, "client", None)
    if session is None:
        raise RuntimeError("Gemini client is not initialized")
    reqid = int(getattr(client, "_reqid", 0) or 0)
    client._reqid = reqid + 100000
    rpc_payload = [USAGE_RPC_ID, "[]", None, "generic"]
    params: dict[str, Any] = {
        "rpcids": USAGE_RPC_ID,
        "source-path": "/usage",
        "hl": getattr(client, "language", None) or "ru",
        "_reqid": reqid,
        "rt": "c",
    }
    build_label = getattr(client, "build_label", None)
    session_id = getattr(client, "session_id", None)
    if build_label:
        params["bl"] = build_label
    if session_id:
        params["f.sid"] = session_id
    return await session.post(
        Endpoint.BATCH_EXEC.value,
        params=params,
        headers=_batch_headers_for_usage(client),
        data={
            "f.req": json.dumps([[rpc_payload]], separators=(",", ":")),
            "at": getattr(client, "access_token", None) or "",
            "": "",
        },
    )


async def _request_usage_body(client: Any) -> Any:
    response = await _request_usage_response(client)
    if response.status_code != 200:
        raise RuntimeError(f"Gemini usage RPC returned HTTP {response.status_code}")
    return _extract_usage_body(response.text)


async def debug_usage_rpc(client: Any) -> dict[str, Any]:
    response = await _request_usage_response(client)
    text = response.text or ""
    try:
        parts = extract_json_from_response(text)
    except Exception as exc:
        parts = []
        parse_error = f"{type(exc).__name__}: {exc}"
    else:
        parse_error = ""
    summary = []
    for part in parts[:20]:
        summary.append({
            "type": type(part).__name__,
            "rpc_id": get_nested_value(part, [1]),
            "field2_type": type(get_nested_value(part, [2])).__name__,
            "field2_preview": str(get_nested_value(part, [2]))[:1200],
            "part_preview": str(part)[:1600],
        })
    return {
        "status_code": response.status_code,
        "response_length": len(text),
        "parse_error": parse_error,
        "parts_count": len(parts),
        "parts": summary,
        "raw_prefix": text[:4000],
    }


async def get_usage_info(client: Any, *, force: bool = False, cache_ttl: float = DEFAULT_CACHE_TTL) -> dict[str, Any]:
    key = id(client)
    now = time.monotonic()
    cached = _cache.get(key)
    if not force and cached and now - cached[0] < cache_ttl:
        result = dict(cached[1]); result["cached"] = True; return result
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        now = time.monotonic(); cached = _cache.get(key)
        if not force and cached and now - cached[0] < cache_ttl:
            result = dict(cached[1]); result["cached"] = True; return result
        try:
            body = await _request_usage_body(client)
            result = {"available": False, "reason": "usage_info_not_returned"} if body is None else parse_usage_body(body)
            result["fetched_at"] = datetime.now(tz=timezone.utc).isoformat(); result["cached"] = False
            _cache[key] = (time.monotonic(), dict(result)); return result
        except Exception as exc:
            if cached:
                result = dict(cached[1]); result.update({"cached": True, "stale": True, "refresh_error": str(exc)[:240]}); return result
            return {"available": False, "cached": False, "reason": "usage_fetch_failed", "error": str(exc)[:240], "fetched_at": datetime.now(tz=timezone.utc).isoformat()}


def clear_usage_cache(client: Any | None = None) -> None:
    if client is None:
        _cache.clear(); _locks.clear(); _usage_session_ids.clear(); return
    key = id(client); _cache.pop(key, None); _locks.pop(key, None); _usage_session_ids.pop(key, None)


def session_snapshot(slot: Any) -> dict[str, Any]:
    store = getattr(slot, "chat_sessions", None)
    if store is None:
        return {"active": 0, "items": []}
    try: store.cleanup()
    except Exception: pass
    now = time.time(); items = []
    for session_id, record in list(getattr(store, "sessions", {}).items()):
        chat = getattr(record, "chat", None)
        cid = getattr(chat, "cid", "") or getattr(getattr(chat, "_chat", None), "cid", "") or ""
        items.append({"session_id": session_id, "cid": cid, "age_seconds": max(0, round(now-record.created_at,1)), "idle_seconds": max(0, round(now-record.last_used_at,1)), "locked": bool(record.lock.locked())})
    items.sort(key=lambda item: item["idle_seconds"]); return {"active": len(items), "items": items}
