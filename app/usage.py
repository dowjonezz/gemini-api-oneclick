"""Gemini account usage/limit diagnostics for the OneClick worker.

This module intentionally keeps the usage-limit backport isolated from the
vendored ``gemini_webapi`` fork. Google exposes the same compute-usage data
shown in Gemini's "Usage & limits" panel through the GetUsageInfo RPC
(``jSf9Qc``). We call that RPC with the already-authenticated GeminiClient and
cache the result briefly so opening the dashboard does not create unnecessary
traffic.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any

from gemini_webapi.constants import Endpoint
from gemini_webapi.utils import extract_json_from_response, get_nested_value

USAGE_RPC_ID = "jSf9Qc"
DEFAULT_CACHE_TTL = 60.0

_TIER_LABELS = {
    1: "FREE",
    2: "PRO",
    3: "ULTRA",
    4: "PLUS",
    6: "ULTRA",
}
_METRIC_WINDOWS = {
    1: ("current_5h", "5h"),
    2: ("weekly", "weekly"),
}

_cache: dict[int, tuple[float, dict[str, Any]]] = {}
_locks: dict[int, asyncio.Lock] = {}


def _utc_iso(timestamp: Any) -> str | None:
    if not isinstance(timestamp, (int, float)) or timestamp <= 0:
        return None
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def parse_usage_body(part_body: Any) -> dict[str, Any]:
    """Normalize one GetUsageInfo response body into a stable UI contract."""
    if not isinstance(part_body, list):
        return {}

    tier_id = get_nested_value(part_body, [0])
    usage_items = get_nested_value(part_body, [1], [])
    use_overage_ai_credits = get_nested_value(part_body, [2])

    result: dict[str, Any] = {
        "available": True,
        "tier": {
            "id": tier_id,
            "label": _TIER_LABELS.get(tier_id) or "UNKNOWN",
        },
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

        metric_label, window = _METRIC_WINDOWS.get(
            metric_type,
            (f"type_{metric_type}", "unknown"),
        )
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
    """Return the first matching GetUsageInfo body from a batchexecute response."""
    for part in extract_json_from_response(response_text):
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


async def _request_usage_body(client: Any) -> Any:
    """Execute GetUsageInfo using the authenticated session owned by ``client``.

    The current OneClick vendored client predates upstream's ``source_path``
    argument for ``_batch_execute``. GetUsageInfo is requested from Gemini's
    ``/usage`` surface, so the small request is reproduced here instead of
    modifying the heavily customized vendored client.
    """
    session = getattr(client, "client", None)
    if session is None:
        raise RuntimeError("Gemini client is not initialized")

    account_index = int(getattr(client, "account_index", 0) or 0)
    reqid = int(getattr(client, "_reqid", 0) or 0)
    client._reqid = reqid + 100000

    # RPCData in this vendored client validates rpcid against its older GRPC
    # enum, so serialize the new upstream RPC directly and keep the backport
    # isolated from the heavily customized vendored library.
    rpc_payload = [USAGE_RPC_ID, "[]", None, "generic"]
    params: dict[str, Any] = {
        "rpcids": USAGE_RPC_ID,
        "_reqid": reqid,
        "rt": "c",
        "source-path": "/usage",
    }
    build_label = getattr(client, "build_label", None)
    session_id = getattr(client, "session_id", None)
    if build_label:
        params["bl"] = build_label
    if session_id:
        params["f.sid"] = session_id

    response = await session.post(
        Endpoint.get_batch_exec_url(account_index),
        params=params,
        data={
            "at": getattr(client, "access_token", None),
            "f.req": json.dumps([[rpc_payload]], separators=(",", ":")),
        },
    )
    if response.status_code != 200:
        raise RuntimeError(f"Gemini usage RPC returned HTTP {response.status_code}")

    return _extract_usage_body(response.text)


async def get_usage_info(
    client: Any,
    *,
    force: bool = False,
    cache_ttl: float = DEFAULT_CACHE_TTL,
) -> dict[str, Any]:
    """Return cached or freshly fetched account usage information."""
    key = id(client)
    now = time.monotonic()
    cached = _cache.get(key)
    if not force and cached and now - cached[0] < cache_ttl:
        result = dict(cached[1])
        result["cached"] = True
        return result

    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        now = time.monotonic()
        cached = _cache.get(key)
        if not force and cached and now - cached[0] < cache_ttl:
            result = dict(cached[1])
            result["cached"] = True
            return result

        try:
            body = await _request_usage_body(client)
            if body is None:
                result = {
                    "available": False,
                    "reason": "usage_info_not_returned",
                }
            else:
                result = parse_usage_body(body)
            result["fetched_at"] = datetime.now(tz=timezone.utc).isoformat()
            result["cached"] = False
            _cache[key] = (time.monotonic(), dict(result))
            return result
        except Exception as exc:  # diagnostics must never break account routing
            if cached:
                result = dict(cached[1])
                result.update(
                    {
                        "cached": True,
                        "stale": True,
                        "refresh_error": str(exc)[:240],
                    }
                )
                return result
            return {
                "available": False,
                "cached": False,
                "reason": "usage_fetch_failed",
                "error": str(exc)[:240],
                "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
            }


def clear_usage_cache(client: Any | None = None) -> None:
    """Clear all usage cache entries or just one client's entry."""
    if client is None:
        _cache.clear()
        _locks.clear()
        return
    key = id(client)
    _cache.pop(key, None)
    _locks.pop(key, None)


def session_snapshot(slot: Any) -> dict[str, Any]:
    """Return safe session diagnostics without message contents."""
    store = getattr(slot, "chat_sessions", None)
    if store is None:
        return {"active": 0, "items": []}

    try:
        store.cleanup()
    except Exception:
        pass

    now = time.time()
    items = []
    for session_id, record in list(getattr(store, "sessions", {}).items()):
        chat = getattr(record, "chat", None)
        cid = getattr(chat, "cid", "") or ""
        if not cid:
            cid = getattr(getattr(chat, "_chat", None), "cid", "") or ""
        items.append(
            {
                "session_id": session_id,
                "cid": cid,
                "age_seconds": max(0, round(now - record.created_at, 1)),
                "idle_seconds": max(0, round(now - record.last_used_at, 1)),
                "locked": bool(record.lock.locked()),
            }
        )
    items.sort(key=lambda item: item["idle_seconds"])
    return {"active": len(items), "items": items}
