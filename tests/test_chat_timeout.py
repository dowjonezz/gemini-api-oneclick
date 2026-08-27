import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "app"), str(ROOT / "lib")]

import worker  # noqa: E402
from fastapi import HTTPException, Response  # noqa: E402
from slot import ChatSessionStore, Slot  # noqa: E402


class HangingChat:
    def __init__(self):
        self.cid = ""
        self.calls = 0

    async def send_message_stream(self, prompt, files=None, tracer=None):
        self.calls += 1
        await asyncio.sleep(3600)
        if False:  # pragma: no cover - keeps this an async generator
            yield None


class FastChat:
    def __init__(self):
        self.cid = ""
        self.calls = 0

    async def send_message_stream(self, prompt, files=None, tracer=None):
        self.calls += 1
        if not self.cid:
            self.cid = "recovered-cid"
        yield SimpleNamespace(text_delta="ok", thoughts_delta="", images=[])


class FakeClient:
    def __init__(self, chat_factory):
        self._model_registry = {}
        self.chat_factory = chat_factory
        self.chats = []

    def start_chat(self, model=None):
        chat = self.chat_factory()
        self.chats.append(chat)
        return chat


class ChatTimeoutTests(unittest.TestCase):
    def setUp(self):
        worker.slots.clear()
        worker._chat_session_slots.clear()
        self.slot = Slot(num=1)
        self.slot.state["initializing"] = False
        self.slot.chat_sessions = ChatSessionStore(request_timeout=0.02)
        worker.slots[1] = self.slot

    def tearDown(self):
        worker.slots.clear()
        worker._chat_session_slots.clear()

    def call(self, session_id, mode):
        response = Response()
        request = worker.ChatCompletionRequest(
            model="gemini-3-flash",
            messages=[{"role": "user", "content": "timeout test"}],
        )
        return asyncio.run(
            worker.slot_chat_completion(1, request, response, None, session_id, mode)
        ), response

    def test_store_timeout_invalidates_record_and_returns_recovery_contract(self):
        async def scenario():
            store = ChatSessionStore(request_timeout=0.01)
            record = store.create("slow", HangingChat)
            with self.assertRaises(HTTPException) as exc:
                async with record.lock:
                    async for _ in record.chat.send_message_stream("hello"):
                        pass

            self.assertEqual(exc.exception.status_code, 409)
            self.assertEqual(exc.exception.headers["X-OneClick-Chat-Session-Status"], "session_not_found")
            self.assertEqual(exc.exception.headers["X-OneClick-Chat-Session-ID"], "slow")
            self.assertNotIn("slow", store.sessions)
            self.assertTrue(record.invalidated)

        asyncio.run(scenario())

    def test_worker_bootstrap_timeout_drops_session_then_same_id_can_recover(self):
        self.slot.client = FakeClient(HangingChat)

        with self.assertRaises(HTTPException) as exc:
            self.call("timeout-loop", "bootstrap")

        self.assertEqual(exc.exception.status_code, 409)
        self.assertEqual(exc.exception.headers["X-OneClick-Chat-Session-Status"], "session_not_found")
        self.assertNotIn("timeout-loop", self.slot.chat_sessions.sessions)

        # The worker-level affinity entry is cleaned lazily by the normal
        # continuation lookup once it sees that the session store no longer
        # contains the timed-out record.
        with self.assertRaises(HTTPException) as missing:
            self.call("timeout-loop", "continue")
        self.assertEqual(missing.exception.status_code, 409)
        self.assertNotIn("timeout-loop", worker._chat_session_slots)

        # Router recovery uses a full bootstrap with the same logical session
        # id. Prove that the poisoned chat no longer blocks that recovery.
        self.slot.client = FakeClient(FastChat)
        result, response = self.call("timeout-loop", "bootstrap")
        self.assertEqual(response.headers["x-oneclick-chat-session-status"], "new")
        self.assertEqual(result["choices"][0]["message"]["content"], "ok")
        self.assertEqual(self.slot.chat_sessions.sessions["timeout-loop"].chat.cid, "recovered-cid")

    def test_invalid_timeout_configuration_is_rejected(self):
        with self.assertRaises(ValueError):
            ChatSessionStore(request_timeout=0)


if __name__ == "__main__":
    unittest.main()
