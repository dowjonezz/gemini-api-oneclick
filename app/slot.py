# -------------------- Slot: per-account GeminiClient lifecycle --------------------
# Each Slot is a "virtual container" — holds one GeminiClient, its state, and sessions.

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException
from gemini_webapi import GeminiClient
from gemini_webapi.constants import AccountStatus

logger = logging.getLogger(__name__)

_SESSION_TTL = 600   # 10 minutes
_MAX_SESSIONS = 50   # per slot
_TLS_RESTART_THRESHOLD = 3
_DEFAULT_TEXT_CHAT_TIMEOUT_SECONDS = 90.0


def _text_chat_timeout_seconds() -> float:
    raw = os.environ.get("TEXT_CHAT_TIMEOUT_SECONDS", str(_DEFAULT_TEXT_CHAT_TIMEOUT_SECONDS))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid TEXT_CHAT_TIMEOUT_SECONDS=%r; using %.0fs",
            raw,
            _DEFAULT_TEXT_CHAT_TIMEOUT_SECONDS,
        )
        return _DEFAULT_TEXT_CHAT_TIMEOUT_SECONDS
    if value <= 0:
        logger.warning(
            "TEXT_CHAT_TIMEOUT_SECONDS must be > 0; using %.0fs",
            _DEFAULT_TEXT_CHAT_TIMEOUT_SECONDS,
        )
        return _DEFAULT_TEXT_CHAT_TIMEOUT_SECONDS
    return value


def _session_not_found(session_id: str, detail: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail=detail,
        headers={
            "X-OneClick-Chat-Session-Status": "session_not_found",
            "X-OneClick-Chat-Session-ID": session_id,
        },
    )


@dataclass
class StoredChatSession:
    """An in-memory Gemini Web chat and the affinity data around it."""

    chat: Any
    created_at: float
    last_used_at: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    invalidated: bool = False


class _TimedChatSession:
    """Proxy a Gemini ChatSession with a hard watchdog for text turns.

    A timed-out Gemini request cannot be safely continued: Google may have
    accepted part of the request even though no complete response reached us.
    Invalidate the local session and return the same 409 contract used for an
    expired/missing session so the router can recover with a full bootstrap.
    """

    def __init__(
        self,
        chat: Any,
        store: "ChatSessionStore",
        session_id: str,
        record: StoredChatSession,
        timeout: float,
    ) -> None:
        self._chat = chat
        self._store = store
        self._session_id = session_id
        self._record = record
        self._timeout = timeout

    def __getattr__(self, name: str) -> Any:
        return getattr(self._chat, name)

    async def send_message_stream(self, *args, **kwargs):
        if self._record.invalidated:
            raise _session_not_found(
                self._session_id,
                "Chat session is no longer valid; send a bootstrap request with full context",
            )
        try:
            async with asyncio.timeout(self._timeout):
                async for output in self._chat.send_message_stream(*args, **kwargs):
                    yield output
        except TimeoutError as exc:
            self._store.invalidate(self._session_id, self._record)
            logger.warning(
                "Gemini text chat timed out session_id=%s timeout=%.1fs; session invalidated",
                self._session_id,
                self._timeout,
            )
            raise _session_not_found(
                self._session_id,
                f"Gemini text chat timed out after {self._timeout:g}s; send a bootstrap request with full context",
            ) from exc


class ChatSessionStore:
    """Bounded, TTL-based storage for ChatSession objects.

    A store belongs to exactly one GeminiClient (or worker Slot), so retaining a
    ChatSession here also pins it to the Google account that created it.
    """

    def __init__(
        self,
        ttl: float = _SESSION_TTL,
        max_sessions: int = _MAX_SESSIONS,
        request_timeout: float | None = None,
    ):
        self.ttl = ttl
        self.max_sessions = max_sessions
        self.request_timeout = _text_chat_timeout_seconds() if request_timeout is None else request_timeout
        if self.request_timeout <= 0:
            raise ValueError("request_timeout must be > 0")
        self.sessions: dict[str, StoredChatSession] = {}

    def clear(self) -> None:
        self.sessions.clear()

    def cleanup(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        expired = [
            session_id
            for session_id, record in self.sessions.items()
            if record.invalidated or (now - record.last_used_at > self.ttl and not record.lock.locked())
        ]
        for session_id in expired:
            del self.sessions[session_id]
        self._evict_to_limit()

    def create(
        self,
        session_id: str,
        factory: Callable[[], Any],
        now: float | None = None,
    ) -> StoredChatSession:
        """Create a fresh session for an explicit bootstrap request."""
        now = time.time() if now is None else now
        self.cleanup(now)
        raw_chat = factory()
        record = StoredChatSession(chat=raw_chat, created_at=now, last_used_at=now)
        record.chat = _TimedChatSession(raw_chat, self, session_id, record, self.request_timeout)
        self.sessions[session_id] = record
        self._evict_to_limit()
        return record

    def get_active(self, session_id: str, now: float | None = None) -> StoredChatSession | None:
        """Return a live session for an explicit continuation, otherwise None."""
        now = time.time() if now is None else now
        record = self.sessions.get(session_id)
        if record is None:
            return None
        if record.invalidated:
            del self.sessions[session_id]
            return None
        # Do not invalidate a stream in progress. The caller will wait on its
        # per-session lock and then continue the same Gemini conversation.
        if record.lock.locked() or now - record.last_used_at <= self.ttl:
            record.last_used_at = now
            return record
        del self.sessions[session_id]
        return None

    def touch(self, record: StoredChatSession, now: float | None = None) -> None:
        if not record.invalidated:
            record.last_used_at = time.time() if now is None else now

    def invalidate(self, session_id: str, record: StoredChatSession) -> None:
        """Drop a poisoned/timed-out session without touching a replacement."""
        record.invalidated = True
        if self.sessions.get(session_id) is record:
            del self.sessions[session_id]

    def _evict_to_limit(self) -> None:
        overflow = len(self.sessions) - self.max_sessions
        if overflow <= 0:
            return
        candidates = sorted(
            (
                (session_id, record)
                for session_id, record in self.sessions.items()
                if not record.lock.locked()
            ),
            key=lambda item: item[1].last_used_at,
        )
        for session_id, _ in candidates[:overflow]:
            del self.sessions[session_id]


def parse_env_file(path: Path) -> dict[str, str]:
    """Read KEY=VALUE lines from an env file."""
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        data[k.strip()] = v.strip()
    return data


@dataclass
class Slot:
    num: int
    psid: str = ""
    psidts: str = ""
    client: GeminiClient | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    state: dict[str, Any] = field(default_factory=lambda: {
        "auth_status": "unknown",
        "last_error": "",
        "last_error_type": "",
        "needs_restart": False,
        "initializing": True,
        "_tls_fail_count": 0,
    })
    edit_sessions: dict[str, Any] = field(default_factory=dict)
    chat_sessions: ChatSessionStore = field(default_factory=ChatSessionStore)

    # ── Factory ──────────────────────────────────────────────────────

    @classmethod
    def from_env_file(cls, num: int, env_path: Path) -> "Slot":
        data = parse_env_file(env_path)
        return cls(
            num=num,
            psid=data.get("SECURE_1PSID", ""),
            psidts=data.get("SECURE_1PSIDTS", ""),
        )

    # ── Lifecycle ────────────────────────────────────────────────────

    async def init_client(self, proxy: str | None = None) -> None:
        """Create and initialise the GeminiClient for this slot."""
        if not self.psid or not self.psidts:
            self.state["auth_status"] = "no_credentials"
            self.state["initializing"] = False
            logger.warning("Slot %d: no credentials, skipping init", self.num)
            return

        try:
            self.state["initializing"] = True
            self.client = GeminiClient(self.psid, self.psidts, proxy=proxy)
            await self.client.init(timeout=300, watchdog_timeout=180, auto_refresh=False)

            status = getattr(self.client, "account_status", None)
            self.state["auth_status"] = status.name if status else "unknown"
            if status and status != AccountStatus.AVAILABLE:
                logger.warning("Slot %d: auth status %s", self.num, status.name)

            self.state["initializing"] = False
            self.state["needs_restart"] = False
            self.state["_tls_fail_count"] = 0
            self.state["last_error"] = ""
            self.state["last_error_type"] = ""
            logger.info("Slot %d: client initialised", self.num)

        except Exception as e:
            logger.error("Slot %d: init failed: %s", self.num, e)
            self.client = None
            self.state["initializing"] = False
            error_str = str(e).lower()
            if "tls" in error_str or "ssl" in error_str or "curl: (35)" in error_str:
                self.state["_tls_fail_count"] += 1
                self.state["last_error_type"] = "tls_error"
                if self.state["_tls_fail_count"] >= _TLS_RESTART_THRESHOLD:
                    self.state["needs_restart"] = True
            else:
                self.state["last_error_type"] = "init_failed"
            self.state["last_error"] = str(e)[:200]
            raise

    async def reload(self, psid: str | None = None, psidts: str | None = None) -> None:
        """Destroy current client and reinitialise with (optionally new) credentials."""
        if psid is not None:
            self.psid = psid
        if psidts is not None:
            self.psidts = psidts
        self.client = None
        self.edit_sessions.clear()
        self.chat_sessions.clear()
        self.state.update({
            "auth_status": "unknown",
            "last_error": "",
            "last_error_type": "",
            "needs_restart": False,
            "_tls_fail_count": 0,
            "initializing": True,
        })
        proxy = os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY") or None
        await self.init_client(proxy=proxy)

    async def reload_from_env(self, envs_dir: Path) -> None:
        """Re-read the env file and reload the client."""
        env_path = envs_dir / f"account{self.num}.env"
        data = parse_env_file(env_path)
        await self.reload(
            psid=data.get("SECURE_1PSID", ""),
            psidts=data.get("SECURE_1PSIDTS", ""),
        )

    # ── Health ───────────────────────────────────────────────────────

    def detect_tier(self) -> dict[str, Any]:
        if not self.client:
            return {"capacity": 0, "label": "unknown"}
        registry = getattr(self.client, "_model_registry", None)
        if not registry:
            return {"capacity": 0, "label": "unknown"}
        cap = max((m.capacity for m in registry.values()), default=0)
        label = {4: "plus", 3: "ultra", 2: "pro", 1: "free"}.get(cap, "unknown")
        result: dict[str, Any] = {"capacity": cap, "label": label, "models": len(registry)}
        status = getattr(self.client, "account_status", None)
        if status and status != AccountStatus.AVAILABLE:
            result["account_status"] = status.name
        return result

    def health_response(self) -> dict[str, Any]:
        """Return the same JSON shape that main.py /health returns."""
        auth = self.state["auth_status"]
        client_ready = self.client is not None

        if self.state["initializing"]:
            status = "initializing"
        elif not client_ready:
            status = "no_client"
        else:
            status = "healthy"

        return {
            "status": status,
            "client_ready": client_ready,
            "auth_status": auth,
            "tier": self.detect_tier(),
            "needs_restart": self.state["needs_restart"],
            "last_error": self.state["last_error"],
            "last_error_type": self.state["last_error_type"],
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        }

    # ── Error reporting ──────────────────────────────────────────────

    def report_error(self, e: Exception) -> None:
        """Classify error and update this slot's state."""
        from gemini_webapi.exceptions import (
            AuthError, RateLimitExceeded, UsageLimitExceeded,
            ImageGenerationBlocked, TemporarilyBlocked,
        )
        error_str = str(e)
        error_lower = error_str.lower()

        if isinstance(e, ImageGenerationBlocked):
            self.state["last_error_type"] = "image_blocked"
        elif isinstance(e, AuthError):
            self.state["last_error_type"] = "cookie_expired"
        elif isinstance(e, UsageLimitExceeded):
            self.state["last_error_type"] = "usage_limit"
        elif isinstance(e, RateLimitExceeded):
            self.state["last_error_type"] = "rate_limit"
        elif isinstance(e, TemporarilyBlocked):
            self.state["last_error_type"] = "temporarily_blocked"
        elif "tls" in error_lower or "ssl" in error_lower or "curl: (35)" in error_lower:
            self.state["last_error_type"] = "tls_error"
            self.state["_tls_fail_count"] += 1
            if self.state["_tls_fail_count"] >= _TLS_RESTART_THRESHOLD:
                self.state["needs_restart"] = True
        elif any(kw in error_lower for kw in ["can't generate more videos", "video generation isn't available"]):
            self.state["last_error_type"] = "video_quota"
        elif any(kw in error_lower for kw in ['401', '403', 'cookie', 'expired']):
            self.state["last_error_type"] = "cookie_expired"
        elif '429' in error_lower or 'rate limit' in error_lower:
            self.state["last_error_type"] = "rate_limit"
        else:
            self.state["last_error_type"] = "unknown"

        self.state["last_error"] = error_str[:200]

        if self.state["last_error_type"] != "tls_error":
            self.state["_tls_fail_count"] = 0

        logger.info("Slot %d error: type=%s msg=%s", self.num, self.state['last_error_type'], error_str[:100])

    # ── Edit sessions ────────────────────────────────────────────────

    def cleanup_expired_sessions(self) -> None:
        now = time.time()
        expired = [sid for sid, (_, ts) in self.edit_sessions.items() if now - ts > _SESSION_TTL]
        for sid in expired:
            del self.edit_sessions[sid]
        if len(self.edit_sessions) > _MAX_SESSIONS:
            by_age = sorted(self.edit_sessions.items(), key=lambda x: x[1][1])
            for sid, _ in by_age[:len(self.edit_sessions) - _MAX_SESSIONS]:
                del self.edit_sessions[sid]
