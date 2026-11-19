"""Offline tests for allowlisted private-message capture and worker lifecycle."""
import asyncio
import json
import unittest
from types import SimpleNamespace

import test_plugin  # installs the minimal SDK stub and imports plugin.py


class MemoryCapturePluginTests(unittest.IsolatedAsyncioTestCase):
    def message(self, **changes):
        msg = {
            "platform": "wx",
            "session_id": "wx:owner-1:private",
            "message_id": "m-1",
            "group_info": None,
            "message_info": {"group_info": None, "user_info": {"user_id": "owner-1"}},
            "raw_message": [{"type": "text", "data": {"text": "我喜欢喝茶"}}],
        }
        msg.update(changes)
        return msg

    def plugin(self, enabled=True):
        inst = test_plugin.mod.DeAiFilterPlugin()
        inst.config = {
            "memory_capture": {
                "enabled": enabled,
                "model_task_name": "memory_extract",
                "allowed_targets": ["wx:owner-1:private"],
                "max_queue": 2,
            },
        }
        return inst

    async def test_default_config_is_disabled(self):
        inst = test_plugin.mod.DeAiFilterPlugin()
        inst.config = test_plugin.CONFIG
        inst._start_capture_worker()
        self.assertIsNone(inst._capture_queue)
        self.assertIsNone(inst._capture_worker)

    async def test_only_allowlisted_private_text_enters_worker_and_stays_pending(self):
        inst = self.plugin()
        inst.ctx = SimpleNamespace(llm=SimpleNamespace(generate=self.fake_generate))
        inst._start_capture_worker()
        await inst.capture_private_message(self.message())
        self.assertEqual(inst._capture_queue.queue.qsize(), 1)
        await inst._capture_queue.process_one()
        candidates = list(inst._capture_store._items.values())
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].status, "pending")
        self.assertEqual(candidates[0].evidence_quotes, ("我喜欢喝茶",))
        await inst.on_unload()

    async def fake_generate(self, **kwargs):
        self.assertEqual(kwargs["task_name"], "memory_extract")
        self.assertEqual(kwargs["temperature"], 0)
        return {"success": True, "response": json.dumps({"candidates": [{
            "domain": "user_fact", "fact": "用户喜欢喝茶", "quote": "我喜欢喝茶",
        }]}, ensure_ascii=False)}

    async def test_rejects_group_media_and_non_allowlisted_session(self):
        inst = self.plugin()
        inst._start_capture_worker()
        await inst.capture_private_message(self.message(group_info={"group_id": "g"}))
        await inst.capture_private_message(self.message(raw_message=[{"type": "image", "data": "x"}]))
        await inst.capture_private_message(self.message(session_id="other"))
        self.assertEqual(inst._capture_queue.queue.qsize(), 0)
        await inst.on_unload()

    async def test_capture_scopes_are_isolated_by_platform_and_user(self):
        inst = self.plugin()
        inst.config["memory_capture"]["allowed_targets"].append("wx:owner-2:private")
        inst._start_capture_worker()
        await inst.capture_private_message(self.message())
        second = self.message(message_id="m-2", session_id="wx:owner-2:private")
        second["message_info"]["user_info"]["user_id"] = "owner-2"
        await inst.capture_private_message(second)
        rows = list(inst._capture_queue.queue._queue)
        self.assertEqual(rows[0][0].owner_id, "wx:owner-1")
        self.assertEqual(rows[1][0].owner_id, "wx:owner-2")
        await inst.on_unload()

    async def test_queue_is_bounded_and_drops_overflow(self):
        inst = self.plugin()
        inst._start_capture_worker()
        await inst.capture_private_message(self.message(message_id="m-1"))
        await inst.capture_private_message(self.message(message_id="m-2"))
        await inst.capture_private_message(self.message(message_id="m-3"))
        self.assertEqual(inst._capture_queue.queue.qsize(), 2)
        await inst.on_unload()

    async def test_config_update_restarts_or_disables_worker(self):
        inst = self.plugin()
        inst._start_capture_worker()
        first = inst._capture_worker
        await inst.on_config_update("self", {}, "2")
        self.assertTrue(first.cancelled())
        self.assertIsNotNone(inst._capture_worker)
        inst.config["memory_capture"]["enabled"] = False
        second = inst._capture_worker
        await inst.on_config_update("self", {}, "3")
        self.assertTrue(second.cancelled())
        self.assertIsNone(inst._capture_worker)
        await inst.on_unload()

    async def test_unload_cancels_worker_and_discards_queued_text(self):
        inst = self.plugin()
        inst.ctx = SimpleNamespace(llm=SimpleNamespace(generate=self.fake_generate))
        inst._start_capture_worker()
        await inst.capture_private_message(self.message())
        worker = inst._capture_worker
        await inst.on_unload()
        self.assertTrue(worker.cancelled())
        self.assertIsNone(inst._capture_queue)
        self.assertEqual(inst._capture_store.count(), 0)


if __name__ == "__main__":
    unittest.main()
