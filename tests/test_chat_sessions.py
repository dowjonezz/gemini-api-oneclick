import asyncio
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "app"), str(ROOT / "lib")]

import worker  # noqa: E402
import main  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi import Response  # noqa: E402
from slot import Slot  # noqa: E402


class FakeChat:
    def __init__(self, number: int, model=None):
        self.number = number
        self.model = model
        self.cid = ""
        self.calls: list[str] = []

    async def send_message_stream(self, prompt, files=None, tracer=None):
        self.calls.append(prompt)
        if not self.cid:
            self.cid = f"gemini-cid-{self.number}"
        yield SimpleNamespace(text_delta=f"reply-{self.number}", thoughts_delta="", images=[])


class FakeClient:
    def __init__(self):
        self._model_registry = {}
        self.chats: list[FakeChat] = []

    def start_chat(self, model=None):
        chat = FakeChat(len(self.chats) + 1, model=model)
        self.chats.append(chat)
        return chat

    async def generate_content_stream(self, *args, **kwargs):
        yield SimpleNamespace(text_delta="legacy", thoughts_delta="", images=[])


def request(messages):
    return worker.ChatCompletionRequest(model="gemini-3-flash", messages=messages)


class WorkerChatSessionTests(unittest.TestCase):
    def setUp(self):
        worker.slots.clear()
        worker._chat_session_slots.clear()
        self.slot1 = Slot(num=1)
        self.slot1.client = FakeClient()
        self.slot1.state["initializing"] = False
        self.slot2 = Slot(num=2)
        self.slot2.client = FakeClient()
        self.slot2.state["initializing"] = False
        worker.slots.update({1: self.slot1, 2: self.slot2})

    def tearDown(self):
        worker.slots.clear()
        worker._chat_session_slots.clear()

    def call(self, slot, messages, session_id, session_mode="bootstrap"):
        response = Response()
        result = asyncio.run(
            worker.slot_chat_completion(
                slot, request(messages), response, None, session_id, session_mode,
            )
        )
        return result, response

    def test_three_requests_reuse_one_chat_and_only_send_new_tool_result(self):
        initial = [
            {"role": "system", "content": "Use the supplied tools."},
            {"role": "user", "content": "Inspect the repository."},
        ]
        _, first_response = self.call(1, initial, "kilo-loop-1", "bootstrap")
        _, second_response = self.call(
            1, [{"role": "tool", "name": "read", "content": "main.py contents"}], "kilo-loop-1", "continue"
        )
        _, third_response = self.call(
            1, [{"role": "tool", "name": "grep", "content": "two matches"}], "kilo-loop-1", "continue"
        )

        self.assertEqual([first_response.headers["x-oneclick-chat-session-status"],
                          second_response.headers["x-oneclick-chat-session-status"],
                          third_response.headers["x-oneclick-chat-session-status"]],
                         ["new", "reused", "reused"])
        self.assertEqual(len(self.slot1.client.chats), 1)
        chat = self.slot1.client.chats[0]
        self.assertEqual(chat.cid, "gemini-cid-1")
        self.assertEqual(len(chat.calls), 3)
        self.assertIn("System: Use the supplied tools.", chat.calls[0])
        self.assertEqual(chat.calls[1], "Tool result (read): main.py contents\n\nAssistant: ")
        self.assertEqual(chat.calls[2], "Tool result (grep): two matches\n\nAssistant: ")

    def test_different_session_creates_different_chat(self):
        self.call(1, [{"role": "user", "content": "first"}], "conversation-a", "bootstrap")
        self.call(1, [{"role": "user", "content": "second"}], "conversation-b", "bootstrap")

        self.assertEqual(len(self.slot1.client.chats), 2)
        self.assertNotEqual(self.slot1.client.chats[0], self.slot1.client.chats[1])

    def test_expired_continuation_returns_409_and_bootstrap_creates_a_new_chat(self):
        self.call(1, [{"role": "user", "content": "first"}], "expires-soon", "bootstrap")
        record = self.slot1.chat_sessions.sessions["expires-soon"]
        record.last_used_at = time.time() - self.slot1.chat_sessions.ttl - 1
        send_count = len(self.slot1.client.chats[0].calls)

        with self.assertRaises(HTTPException) as exc:
            self.call(1, [{"role": "tool", "content": "late result"}], "expires-soon", "continue")

        self.assertEqual(exc.exception.status_code, 409)
        self.assertEqual(exc.exception.headers["X-OneClick-Chat-Session-Status"], "session_not_found")
        self.assertEqual(len(self.slot1.client.chats[0].calls), send_count)

        _, response = self.call(1, [{"role": "user", "content": "full bootstrap"}], "expires-soon", "bootstrap")
        self.assertEqual(response.headers["x-oneclick-chat-session-status"], "new")
        self.assertEqual(len(self.slot1.client.chats), 2)
        self.assertEqual(self.slot1.client.chats[1].cid, "gemini-cid-2")

    def test_session_is_pinned_to_its_creating_slot(self):
        self.call(1, [{"role": "user", "content": "pin me"}], "pinned-loop", "bootstrap")

        with self.assertRaises(HTTPException) as exc:
            self.call(2, [{"role": "tool", "content": "must not move"}], "pinned-loop", "continue")

        self.assertEqual(exc.exception.status_code, 409)
        self.assertEqual(exc.exception.headers["X-OneClick-Chat-Session-Status"], "slot_mismatch")
        self.assertEqual(len(self.slot2.client.chats), 0)


class MainChatSessionTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.previous_client = main.gemini_client
        main.gemini_client = self.client
        main._chat_sessions.clear()

    def tearDown(self):
        main.gemini_client = self.previous_client
        main._chat_sessions.clear()

    def test_main_endpoint_reuses_the_same_chat_session(self):
        first_response = Response()
        asyncio.run(main.create_chat_completion(
            main.ChatCompletionRequest(model="gemini-3-flash", messages=[{"role": "user", "content": "start"}]),
            first_response, None, "main-loop", "bootstrap",
        ))
        second_response = Response()
        asyncio.run(main.create_chat_completion(
            main.ChatCompletionRequest(model="gemini-3-flash", messages=[{"role": "tool", "content": "result"}]),
            second_response, None, "main-loop", "continue",
        ))

        self.assertEqual(first_response.headers["x-oneclick-chat-session-status"], "new")
        self.assertEqual(second_response.headers["x-oneclick-chat-session-status"], "reused")
        self.assertEqual(len(self.client.chats), 1)
        self.assertEqual(self.client.chats[0].calls[1], "Tool result: result\n\nAssistant: ")


if __name__ == "__main__":
    unittest.main()
