import asyncio
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "app"), str(ROOT / "lib")]

import usage  # noqa: E402


class UsageParsingTests(unittest.TestCase):
    def tearDown(self):
        usage.clear_usage_cache()

    def test_parse_pro_5h_weekly_and_ai_credits(self):
        body = [
            2,
            [
                [870, 0.13, 1, [[1_788_000_000]]],
                [980, 0.02, 2, [[1_788_500_000]]],
                [123, 0.0, 3, [[1_788_500_000]]],
            ],
            True,
        ]
        parsed = usage.parse_usage_body(body)
        self.assertTrue(parsed["available"])
        self.assertEqual(parsed["tier"], {"id": 2, "label": "PRO"})
        self.assertEqual(parsed["current_5h"]["usage_percentage"], 13)
        self.assertEqual(parsed["current_5h"]["remaining_credits"], 870)
        self.assertEqual(parsed["weekly"]["usage_percentage"], 2)
        self.assertEqual(parsed["ai_credits_remaining"], 123)
        self.assertTrue(parsed["current_5h"]["reset_at"].endswith("+00:00"))

    def test_usage_percentage_is_clamped_for_bad_server_values(self):
        parsed = usage.parse_usage_body([1, [[0, 1.37, 1, [[1_788_000_000]]]], False])
        self.assertEqual(parsed["current_5h"]["usage_percentage"], 100)

    def test_cache_prevents_repeated_google_rpc_within_ttl(self):
        client = object()
        body = [4, [[50, 0.25, 1, [[1_788_000_000]]]], False]

        async def scenario():
            with patch.object(usage, "_request_usage_body", new=AsyncMock(return_value=body)) as request:
                first = await usage.get_usage_info(client, cache_ttl=60)
                second = await usage.get_usage_info(client, cache_ttl=60)
                self.assertEqual(request.await_count, 1)
                self.assertFalse(first["cached"])
                self.assertTrue(second["cached"])
                self.assertEqual(second["tier"]["label"], "PLUS")

        asyncio.run(scenario())

    def test_force_bypasses_cache(self):
        client = object()
        body = [1, [], False]

        async def scenario():
            with patch.object(usage, "_request_usage_body", new=AsyncMock(return_value=body)) as request:
                await usage.get_usage_info(client)
                await usage.get_usage_info(client, force=True)
                self.assertEqual(request.await_count, 2)

        asyncio.run(scenario())

    def test_stale_cache_survives_refresh_failure(self):
        client = object()
        body = [2, [[25, 0.5, 2, [[1_788_000_000]]]], False]

        async def scenario():
            with patch.object(usage, "_request_usage_body", new=AsyncMock(return_value=body)):
                first = await usage.get_usage_info(client)
            self.assertTrue(first["available"])
            with patch.object(usage, "_request_usage_body", new=AsyncMock(side_effect=RuntimeError("temporary"))):
                stale = await usage.get_usage_info(client, force=True)
            self.assertTrue(stale["available"])
            self.assertTrue(stale["cached"])
            self.assertTrue(stale["stale"])
            self.assertIn("temporary", stale["refresh_error"])

        asyncio.run(scenario())

    def test_session_snapshot_never_exposes_message_content(self):
        now = time.time()
        record = SimpleNamespace(
            created_at=now - 100,
            last_used_at=now - 5,
            lock=SimpleNamespace(locked=lambda: False),
            chat=SimpleNamespace(cid="c_abc"),
        )
        store = SimpleNamespace(sessions={"router-session-1": record}, cleanup=lambda: None)
        slot = SimpleNamespace(chat_sessions=store)
        snap = usage.session_snapshot(slot)
        self.assertEqual(snap["active"], 1)
        self.assertEqual(snap["items"][0]["session_id"], "router-session-1")
        self.assertEqual(snap["items"][0]["cid"], "c_abc")
        self.assertNotIn("messages", snap["items"][0])
        self.assertNotIn("prompt", snap["items"][0])


if __name__ == "__main__":
    unittest.main()
