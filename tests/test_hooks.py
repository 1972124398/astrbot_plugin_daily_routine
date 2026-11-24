import asyncio
import json
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot.api.provider import ProviderRequest
from astrbot_plugin_daily_routine.main import DailyRoutine, NAME, TURN
from test_routine import record, stamp


class Event:
    def __init__(self, message="我工作日九点到六点上班", origin="test:FriendMessage:alice", sender="alice", persona="friend"):
        self.unified_msg_origin, self.sender, self.message = origin, sender, message
        self.message_obj = SimpleNamespace(message_id=uuid.uuid4().hex)
        self.extras = {"_trace_persona_id": persona}

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_sender_id(self):
        return self.sender

    def get_sender_name(self):
        return self.sender

    def get_message_str(self):
        return self.message

    def plain_result(self, value):
        return value


class Hooks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.stars = {}
        self.context = SimpleNamespace(
            get_config=lambda **_: {"plugin_set": ["*"]},
            get_registered_star=lambda name: self.stars.get(name),
            get_current_chat_provider_id=AsyncMock(return_value="chat"),
            llm_generate=AsyncMock(return_value=SimpleNamespace(completion_text='{"routines":[],"facts":[],"remove":[]}')),
        )
        with patch("astrbot_plugin_daily_routine.main.StarTools.get_data_dir", return_value=Path(self.temp.name)):
            self.plugin = DailyRoutine(self.context, {"china_calendar": False})
        self.stars[NAME] = SimpleNamespace(activated=True, star_cls=self.plugin)

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.temp.cleanup()

    async def prepare(self, event=None):
        event = event or Event()
        request = ProviderRequest(prompt=event.message, system_prompt="原来的人格")
        await self.plugin.prepare(event, request)
        return event, request

    async def finish(self, event):
        await self.plugin.response(event, SimpleNamespace(completion_text="继续聊", tools_call_args=[]))

    async def test_frontend_only_queues_and_reads_memory(self):
        self.plugin.store.save = lambda _: self.fail("前台不应落盘")
        self.plugin.store.load = lambda: self.fail("前台不应读盘")
        event, request = await self.prepare()
        profile = self.plugin.state["profiles"][event.get_extra(TURN)["key"]]
        self.assertEqual(len(profile["pending"]), 1)
        self.context.llm_generate.assert_not_awaited()
        self.context.get_current_chat_provider_id.assert_not_awaited()
        self.assertEqual(request.system_prompt, "原来的人格")
        # Restore save for teardown.
        self.plugin.store = __import__("astrbot_plugin_daily_routine.store", fromlist=["Store"]).Store(self.temp.name)

    async def test_group_people_and_personas_are_separate(self):
        await self.prepare(Event(origin="test:GroupMessage:g", sender="a"))
        await self.prepare(Event(origin="test:GroupMessage:g", sender="b"))
        await self.prepare(Event(origin="test:GroupMessage:g", sender="a", persona="other"))
        self.assertEqual(len(self.plugin.state["profiles"]), 3)
        self.assertEqual(self.plugin.companion_snapshot("test:GroupMessage:g"), {})
        self.assertEqual(self.plugin.companion_timing("test:GroupMessage:g"), {})

    async def test_reports_and_ai_activity_do_not_become_human_routines(self):
        for source in ("codex_bridge", "self_amusement", "play_together", "daily_routine"):
            event = Event()
            event.set_extra(source, True if source != "codex_bridge" else {"job": "1"})
            await self.prepare(event)
        event = Event()
        event.set_extra("w1_event", {"is_user_message": False})
        await self.prepare(event)
        self.assertFalse(self.plugin.state["profiles"])

    async def test_same_event_tool_round_does_not_collect_or_inject_twice(self):
        event, request = await self.prepare()
        await self.plugin.prepare(event, request)
        self.assertEqual(len(next(iter(self.plugin.state["profiles"].values()))["pending"]), 1)

    async def test_working_user_can_still_chat_and_hint_is_temporary(self):
        event = Event()
        _, p = self.plugin.identity(event)
        p["routines"]["work"] = record()
        with patch("astrbot_plugin_daily_routine.main.time.time", return_value=stamp("2026-11-24T12:00")):
            _, request = await self.prepare(event)
        self.assertEqual(request.system_prompt, "原来的人格")
        self.assertIn("不改变语气", request.extra_user_content_parts[0].text)
        self.assertTrue(request.extra_user_content_parts[0]._no_save)
        self.assertTrue(event.get_extra(TURN))

    async def test_routine_question_can_recall_facts_without_injecting_them_into_unrelated_chat(self):
        event = Event(message="你还记得我几点下班吗")
        _, p = self.plugin.identity(event)
        p["facts"]["work"] = {"key": "work", "text": "通常下午六点下班"}
        with patch("astrbot_plugin_daily_routine.main.time.time", return_value=stamp("2026-11-24T15:00")):
            _, request = await self.prepare(event)
            self.assertIn("下午六点下班", request.extra_user_content_parts[0].text)
            _, unrelated = await self.prepare(Event(message="讲个故事"))
            self.assertFalse(any("下午六点下班" in p.text for p in unrelated.extra_user_content_parts))

    async def test_background_extract_applies_and_survives_reload(self):
        event, _ = await self.prepare()
        key, p = self.plugin.identity(event)
        raw = {"routines": [{**record(), "source_id": p["pending"][0]["id"], "evidence": "九点到六点", "text": "工作日九点到六点"}]}
        self.context.llm_generate.return_value.completion_text = json.dumps(raw)
        await self.plugin.process(key, p)
        self.assertEqual(p["routines"]["work"]["start"], "09:00")
        self.assertFalse(p["pending"])
        await self.plugin.persist()
        self.assertEqual(self.plugin.store.load()["profiles"][key]["routines"], p["routines"])

    async def test_inflight_result_cannot_restore_deleted_profile(self):
        event, _ = await self.prepare()
        key, p = self.plugin.identity(event)

        async def generate(**kwargs):
            _ = [line async for line in self.plugin.control(event, "clear")]
            return SimpleNamespace(completion_text=json.dumps({"facts": [{"key": "old", "text": "旧记录", "source_id": p["pending"][0]["id"], "evidence": "九点到六点"}]}))

        self.context.llm_generate.side_effect = generate
        await self.plugin.process(key, p)
        self.assertFalse(self.plugin.state["profiles"][key]["facts"])

    async def test_new_chat_cancels_shared_analysis_but_keeps_pending(self):
        task = asyncio.create_task(asyncio.sleep(10))
        await asyncio.sleep(0)
        self.plugin.analysis_task = task
        await self.prepare()
        await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(task.cancelled())
        self.assertTrue(next(iter(self.plugin.state["profiles"].values()))["pending"])

    async def test_independent_analysis_is_not_cancelled_by_known_other_provider(self):
        self.plugin.config["analysis_provider_id"] = "background"
        self.plugin.chat_providers[Event().unified_msg_origin] = "chat"
        task = asyncio.create_task(asyncio.sleep(10))
        self.plugin.analysis_task = task
        await self.prepare()
        self.assertFalse(task.cancelling())
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_manual_schedule_replaces_old_same_kind(self):
        event = Event()
        _, p = self.plugin.identity(event)
        p["routines"]["old"] = record()
        result = [r async for r in self.plugin.set_routine(event, "work", "22:00", "06:00", "0,1,2,3,4")]
        self.assertTrue(result)
        self.assertEqual(list(p["routines"]), ["manual_work"])
        self.assertEqual(p["routines"]["manual_work"]["days"], [0, 1, 2, 3, 4])

    async def test_greeting_defaults_off_and_wait_never_sends(self):
        event = Event()
        _, p = self.plugin.identity(event)
        p["last_human"] = stamp("2026-11-23T20:00")
        runtime = SimpleNamespace(enabled=AsyncMock(return_value=True), view=AsyncMock(return_value=SimpleNamespace(persona_id="friend", persona="p")),
                                  draft=AsyncMock(return_value="WAIT"), send=AsyncMock(return_value=True))
        self.plugin.runtime = runtime
        with patch("astrbot_plugin_daily_routine.main.time.time", return_value=stamp("2026-11-24T08:00")):
            await self.plugin.greet(p)
            runtime.draft.assert_not_awaited()
            self.plugin.config.update(proactive_enabled=True, proactive_origins=[event.unified_msg_origin])
            await self.plugin.greet(p)
            runtime.draft.assert_awaited_once()
            runtime.send.assert_not_awaited()
            self.assertTrue(p["offered"])

    async def test_greeting_does_not_repeat_until_human_replies(self):
        event = Event()
        _, p = self.plugin.identity(event)
        p["last_human"] = stamp("2026-11-23T20:00")
        runtime = SimpleNamespace(enabled=AsyncMock(return_value=True), view=AsyncMock(return_value=SimpleNamespace(persona_id="friend", persona="p")),
                                  draft=AsyncMock(return_value="早。"), send=AsyncMock(return_value=True))
        self.plugin.runtime = runtime
        self.plugin.config.update(proactive_enabled=True, proactive_origins=[event.unified_msg_origin])
        with patch("astrbot_plugin_daily_routine.main.time.time", return_value=stamp("2026-11-24T08:00")):
            await self.plugin.greet(p)
        with patch("astrbot_plugin_daily_routine.main.time.time", return_value=stamp("2026-11-25T08:00")):
            await self.plugin.greet(p)
        runtime.send.assert_awaited_once()
        self.assertEqual(p["last_message"], "早。")

    async def test_contact_off_or_persona_changed_cancels_draft(self):
        event = Event()
        _, p = self.plugin.identity(event)
        p["last_human"] = stamp("2026-11-23T20:00")
        self.plugin.config.update(proactive_enabled=True, proactive_origins=[event.unified_msg_origin])
        self.plugin.runtime = SimpleNamespace(enabled=AsyncMock(return_value=True), view=AsyncMock(return_value=SimpleNamespace(persona_id="other", persona="p")),
                                              draft=AsyncMock(return_value="早"), send=AsyncMock())
        with patch("astrbot_plugin_daily_routine.main.time.time", return_value=stamp("2026-11-24T08:00")):
            await self.plugin.greet(p)
        self.plugin.runtime.draft.assert_not_awaited()
        self.plugin.runtime.send.assert_not_awaited()

    async def test_background_failure_keeps_pending_for_retry(self):
        self.plugin.config["learn_idle_seconds"] = 0
        event, _ = await self.prepare()
        await self.finish(event)
        self.plugin.last_front = 0
        self.context.llm_generate.side_effect = ValueError("provider offline")
        self.plugin.worker = asyncio.create_task(self.plugin.run())
        for _ in range(100):
            await asyncio.sleep(0.01)
            p = next(iter(self.plugin.state["profiles"].values()))
            if p["failures"]:
                break
        self.assertEqual(p["failures"], 1)
        self.assertTrue(p["pending"])
        self.assertGreater(p["retry_at"], time.time())
