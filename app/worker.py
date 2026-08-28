"""OneClick worker entrypoint with account usage diagnostics.

The customized worker remains in ``worker_legacy.py`` unchanged. Keeping the
extension layer small makes upstream/session changes easy to audit while adding
new management-only endpoints without touching chat routing semantics.
"""
from __future__ import annotations

import asyncio as _asyncio
from datetime import datetime as _datetime, timezone as _timezone

import worker_legacy as _legacy

globals().update({
    name: value
    for name, value in vars(_legacy).items()
    if not name.startswith("__")
})

from usage import clear_usage_cache as _clear_usage_cache  # noqa: E402
from usage import get_usage_info as _get_usage_info  # noqa: E402
from usage import session_snapshot as _session_snapshot  # noqa: E402


def _account_status_payload(client):
    status = getattr(client, "account_status", None)
    code = None
    try:
        code = int(status) if status is not None else None
    except (TypeError, ValueError):
        pass
    return {
        "name": getattr(status, "name", "UNKNOWN"),
        "code": code,
        "description": getattr(status, "description", ""),
    }


async def _slot_usage_payload(num: int, *, force: bool = False):
    slot = _legacy._get_slot(num)
    if slot.client is None:
        try:
            client = await _legacy._get_client(slot)
        except Exception as exc:
            return {
                "num": num,
                "available": False,
                "reason": "client_unavailable",
                "error": str(exc)[:240],
                "auth_status": slot.state.get("auth_status", "unknown"),
                "sessions": _session_snapshot(slot),
            }
    else:
        client = slot.client

    usage = await _get_usage_info(client, force=force)
    sessions = _session_snapshot(slot)
    return {
        "num": num,
        "available": bool(usage.get("available")),
        "auth_status": slot.state.get("auth_status", "unknown"),
        "account_status": _account_status_payload(client),
        "usage": usage,
        "sessions": sessions,
    }


@app.get("/slot/{num}/usage", dependencies=[Depends(_verify_api_key)])
async def slot_usage(num: int, force: bool = False):
    """Return Google-reported compute limits and safe chat-session diagnostics."""
    return await _slot_usage_payload(num, force=force)


@app.get("/worker/usage", dependencies=[Depends(_verify_api_key)])
async def worker_usage(force: bool = False):
    """Return usage diagnostics for every configured slot."""
    semaphore = _asyncio.Semaphore(4)

    async def fetch_one(num: int):
        async with semaphore:
            return await _slot_usage_payload(num, force=force)

    results = await _asyncio.gather(
        *(fetch_one(num) for num in sorted(slots)),
        return_exceptions=True,
    )
    accounts = []
    for num, result in zip(sorted(slots), results, strict=True):
        if isinstance(result, Exception):
            accounts.append({
                "num": num,
                "available": False,
                "reason": "usage_fetch_failed",
                "error": str(result)[:240],
            })
        else:
            accounts.append(result)
    return {
        "accounts": accounts,
        "fetched_at": _datetime.now(tz=_timezone.utc).isoformat(),
    }


@app.post("/worker/usage/refresh", dependencies=[Depends(_verify_api_key)])
async def refresh_worker_usage():
    """Drop the local usage cache; the next read fetches fresh Google values."""
    _clear_usage_cache()
    return {"ok": True}
