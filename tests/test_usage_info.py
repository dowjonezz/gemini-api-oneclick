import asyncio
import json
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "app"), str(ROOT / "lib")]

import usage  # noqa: E402
from gemini_webapi.constants import Headers, MODEL_HEADER_KEY  # noqa: E402


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

    def test_request_uses_usage_source_path_raw_rpc_id_and_batch_headers(self):
        class FakeResponse:
            status_code = 200
            text = ")]}'\n"

        class FakeSession:
            def __init__(self):
                self.calls = []

            async def post(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return FakeResponse()

        client = SimpleNamespace(
            client=FakeSession(),
            account_index=0,
            _reqid=12345,
            build_label="build",
            session_id="sid",
            access_token="token",
        )

        asyncio.run(usage._request_usage_body(client))
        self.assertEqual(len(client.client.calls), 1)
        _, kwargs = client.client.calls[0]
        self.assertEqual(kwargs["params"]["rpcids"], usage.USAGE_RPC_ID)
        self.assertEqual(kwargs["params"]["source-path"], "/usage")
        self.assertEqual(kwargs["params"]["bl"], "build")
        self.assertEqual(kwargs["params"]["f.sid"], "sid")

        payload = json.loads(kwargs["data"]["f.req"])
        self.assertEqual(payload[0][0][0], usage.USAGE_RPC_ID)
        self.assertEqual(payload[0][0][1], "[]")
        self.assertEqual(client._reqid, 112345)

        headers = kwargs["headers"]
        self.assertEqual(headers["X-Same-Domain"], "1")
        self.assertEqual(headers["Origin"], Headers.GEMINI.value["Origin"])
        self.assertEqual(headers["x-goog-ext-73010989-jspb"], "[0]")
        model_header = json.loads(headers[MODEL_HEADER_KEY])
        self.assertEqual(
            model_header[:9],
            [1, None, None, None, None, None, None, None, [4, 5, 6, 8]],
        )
        self.assertEqual(model_header[-3:-1], [1, 1])
        self.assertIsInstance(model_header[-1], str)
        self.assertTrue(model_header[-1])

    def test_usage_batch_session_header_is_stable_per_client_and_cleared(self):
        client = object()
        first = json.loads(usage._batch_headers_for_usage(client)[MODEL_HEADER_KEY])[-1]
        second = json.loads(usage._batch_headers_for_usage(client)[MODEL_HEADER_KEY])[-1]
        self.assertEqual(first, second)

        usage.clear_usage_cache(client)
        third = json.loads(usage._batch_headers_for_usage(client)[MODEL_HEADER_KEY])[-1]
        self.assertNotEqual(first, third)

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
