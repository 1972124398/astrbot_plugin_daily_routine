"""User-facing memory and curiosity scenarios, including upgrades from 0.1."""
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import unittest

import test_hooks as fixtures
from test_hooks import Event
from test_routine import record, stamp
from astrbot_plugin_daily_routine.routine import apply_updates, curiosity_due, parse_updates, recall


def thought(now):
    return {"key": "overtime", "text": "最近总加班，不知道周末能不能歇一歇", "reason": "提过连续加班",
            "evidence": "最近总加班", "source_id": "one", "updated": now, "considered": 0}


class DailyMemory(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.case = fixtures.Hooks()
        await self.case.asyncSetUp()
        self.plugin, self.context = self.case.plugin, self.case.context
        self.now = stamp("2026-11-24T15:00")
        self.event = Event()
        self.key, self.p = self.plugin.identity(self.event)

    async def asyncTearDown(self):
        await self.case.asyncTearDown()

    async def prepare(self, message, now=None):
        event = Event(message=message)
        with patch("astrbot_plugin_daily_routine.main.time.time", return_value=now or self.now):
            _, request = await self.case.prepare(event)
        return event, request

    async def finish(self, event, reply="嗯"):
        with patch("astrbot_plugin_daily_routine.main.time.time", return_value=self.now):
            await self.plugin.response(event, SimpleNamespace(completion_text=reply, tools_call_args=[]))

    async def test_work_time_does_not_add_a_prompt_to_coding_or_story_requests(self):
        self.p["routines"]["work"] = record()
        for message in ("帮我修一下这个 Python 报错", "你好，帮我修一下代码", "讲一个海盗的故事"):
            _, request = await self.prepare(message)
            self.assertEqual(request.system_prompt, "原来的人格")
            self.assertEqual(request.extra_user_content_parts, [])
        self.context.llm_generate.assert_not_awaited()

    async def test_recall_selects_the_relevant_habit_instead_of_first_entries(self):
        self.p["facts"] = {"tea": {"text": "喜欢喝茶", "updated": self.now},
                           "meal": {"text": "早餐一般喝牛奶", "updated": self.now},
                           "sleep": {"text": "通常十一点睡觉", "updated": self.now}}
        _, request = await self.prepare("你还记得我几点睡觉吗")
        content = request.extra_user_content_parts[0].text
        self.assertIn("十一点睡觉", content)
        self.assertNotIn("喝茶", content)
        self.assertNotIn("牛奶", content)

    async def test_expired_temporary_fact_is_not_presented_as_current_life(self):
        self.p["facts"]["trip"] = {"text": "今天出差去上海", "date_from": "2026-11-23", "date_to": "2026-11-23", "updated": self.now - 86400}
        self.assertFalse(recall(self.p, "出差怎么样", self.now, {}))
        self.assertIn("过去", recall(self.p, "之前出差怎么样", self.now, {})[0])
        self.p["facts"]["tomorrow"] = {"text": "明天出差去杭州", "date_from": "2026-11-25", "date_to": "2026-11-25", "updated": self.now}
        self.assertIn("杭州", recall(self.p, "明天出差去哪", self.now, {})[0])
        self.assertFalse(recall(self.p, "今天出差去哪", self.now, {}))

    async def test_fact_can_be_refined_to_a_schedule_without_two_conflicting_records(self):
        self.p["facts"]["work"] = {"text": "大概六点下班", "updated": self.now - 100}
        update = {"routines": [{**record(), "text": "工作日九点到六点上班"}], "facts": [], "remove": []}
        apply_updates(self.p, update, self.now)
        self.assertNotIn("work", self.p["facts"])
        self.assertIn("六点下班", self.p["routines"]["work"]["previous"][0]["text"])
        self.assertIn("更早提过", recall(self.p, "以前几点下班", self.now, {})[0])

    async def test_a_short_answer_is_understood_with_the_previous_actual_question(self):
        event, _ = await self.prepare("最近加班挺多")
        await self.finish(event, "一般忙到几点下班？")
        self.context.llm_generate.return_value.completion_text = '{"routines":[],"facts":[],"remove":[]}'
        await self.plugin.process(self.key, self.p)
        event, _ = await self.prepare("六点左右")
        await self.finish(event, "嗯，记着了")
        source = self.p["pending"][0]["id"]
        self.context.llm_generate.return_value.completion_text = json.dumps({"facts": [
            {"key": "off_work", "text": "通常六点左右下班", "source_id": source, "evidence": "六点左右"}], "curiosity": None})
        await self.plugin.process(self.key, self.p)
        payload = json.loads(self.context.llm_generate.call_args.kwargs["prompt"])
        self.assertEqual(payload["recent_dialogue"][-1]["reply"], "一般忙到几点下班？")
        self.assertEqual(payload["turns"][0]["text"], "六点左右")
        self.assertEqual(self.p["facts"]["off_work"]["text"], "通常六点左右下班")
        self.assertFalse(self.p["curiosity"])

    async def test_ai_suggestion_and_old_context_cannot_be_used_as_new_human_evidence(self):
        self.p["recent"] = [{"id": "old", "text": "今天加班", "reply": "你可能早上九点上班", "time": self.now - 100}]
        turns = [{"id": "new", "text": "嗯，先聊点别的"}]
        parsed = parse_updates(json.dumps({"facts": [{"key": "shift", "text": "九点上班", "source_id": "new", "evidence": "早上九点上班"}]}), turns)
        self.assertFalse(parsed["facts"])

    async def test_curiosity_requires_a_specific_human_occasion(self):
        turns = [{"id": "one", "text": "最近总加班"}]
        data = {"routines": [], "curiosity": {"key": "overtime", "text": "想知道周末能不能休息", "reason": "连续加班", "source_id": "one", "evidence": "最近总加班"}}
        parsed = parse_updates(json.dumps(data), turns)
        apply_updates(self.p, parsed, self.now)
        self.assertEqual(curiosity_due(self.p, self.now, {})["key"], "overtime")
        data["curiosity"]["evidence"] = "你应该告诉我几点睡觉"
        self.assertNotIn("curiosity", parse_updates(json.dumps(data), turns))

    async def test_curiosity_is_optional_and_does_not_interrupt_unrelated_work(self):
        self.p["curiosity"] = thought(self.now)
        _, request = await self.prepare("帮我修 Python 报错")
        self.assertEqual(request.extra_user_content_parts, [])
        event, request = await self.prepare("最近加班实在太多了")
        self.assertFalse(any("一点好奇" in p.text for p in request.extra_user_content_parts))
        self.assertEqual(request.system_prompt, "原来的人格")
        await self.finish(event)
        _, again = await self.prepare("下班了")
        self.assertFalse(any("一点好奇" in part.text for part in again.extra_user_content_parts))

    async def test_rephrasing_does_not_reset_curiosity_cooldown_and_a_reply_can_resolve_it(self):
        old = thought(self.now)
        old["considered"] = self.now
        self.p["curiosity"] = old
        update = {"routines": [], "facts": [], "remove": [], "curiosity": {**old, "text": "周末会不会有空"}}
        apply_updates(self.p, update, self.now + 10)
        self.assertIsNone(curiosity_due(self.p, self.now + 60, {}))
        apply_updates(self.p, {"routines": [], "facts": [], "remove": [], "curiosity": None}, self.now + 20)
        self.assertIsNone(curiosity_due(self.p, self.now + 4 * 86400, {}))

    async def test_greeting_opportunity_does_not_appear_mid_conversation(self):
        morning = stamp("2026-11-24T08:00")
        self.p["last_human"] = morning - 120
        _, request = await self.prepare("早安", morning)
        self.assertEqual(request.extra_user_content_parts, [])
        self.p["last_human"] = morning - 3600
        self.p["pending"] = []
        _, request = await self.prepare("早安", morning)
        self.assertIn("早间问候", request.extra_user_content_parts[0].text)

    async def test_curiosity_can_be_considered_in_a_reopening_without_becoming_a_fixed_question(self):
        self.p["curiosity"] = thought(self.now)
        self.p["last_human"] = self.now - 3600
        _, request = await self.prepare("嗨")
        self.assertIn("此刻也想了解", request.extra_user_content_parts[0].text)
        self.assertIn("不必提起或追问", request.extra_user_content_parts[0].text)
        self.plugin.config["curiosity_enabled"] = False
        self.p["last_human"] = self.now - 3600
        _, request = await self.prepare("嗨")
        self.assertFalse(any("一点好奇" in part.text for part in request.extra_user_content_parts))

    async def test_proactive_curiosity_uses_existing_permission_mood_and_wait_path(self):
        self.p["curiosity"] = thought(self.now)
        self.p["last_human"] = self.now - 3600
        self.plugin.runtime = SimpleNamespace(enabled=AsyncMock(return_value=True),
            view=AsyncMock(return_value=SimpleNamespace(persona_id="friend", persona="原人格")),
            draft=AsyncMock(return_value="WAIT"), send=AsyncMock())
        with patch("astrbot_plugin_daily_routine.main.time.time", return_value=self.now):
            await self.plugin.greet(self.p)
            self.plugin.runtime.draft.assert_not_awaited()
            self.plugin.config.update(proactive_enabled=True, proactive_origins=[self.event.unified_msg_origin])
            await self.plugin.greet(self.p)
            payload = self.plugin.runtime.draft.call_args.args[2]
            self.assertEqual(payload["opportunities"][0]["label"], "可选的日常好奇")
            self.plugin.runtime.send.assert_not_awaited()
            self.assertEqual(self.p["curiosity"]["considered"], self.now)

    async def test_proactive_curiosity_waits_until_recent_answers_are_processed(self):
        self.p["curiosity"] = thought(self.now)
        self.p["last_human"] = self.now - 3600
        self.p["pending"] = [{"id": "answer", "text": "周末可以休息", "time": self.now - 3600}]
        self.plugin.config.update(proactive_enabled=True, proactive_origins=[self.event.unified_msg_origin])
        self.plugin.runtime = SimpleNamespace(draft=AsyncMock(), send=AsyncMock())
        with patch("astrbot_plugin_daily_routine.main.time.time", return_value=self.now):
            await self.plugin.greet(self.p)
        self.plugin.runtime.draft.assert_not_awaited()

    async def test_sent_curiosity_and_answer_survive_restart_as_context(self):
        self.p["curiosity"] = thought(self.now)
        self.p.update(last_sent=self.now - 60, last_human=self.now - 3600, last_message="今天加班忙到几点呀？")
        await self.plugin.persist()
        loaded = self.plugin.store.load()["profiles"][self.key]
        self.assertEqual(loaded["curiosity"]["key"], "overtime")
        event, _ = await self.prepare("六点左右")
        self.assertEqual(self.p["pending"][0]["preceding_reply"], "今天加班忙到几点呀？")
        await self.finish(event)

    async def test_forget_also_removes_context_that_could_recreate_the_old_memory(self):
        self.p["facts"]["work"] = {"text": "总加班"}
        self.p["recent"] = [{"id": "old", "text": "总加班"}]
        self.p["curiosity"] = thought(self.now)
        _ = [r async for r in self.plugin.forget(self.event, "work")]
        self.assertFalse(self.p["facts"])
        self.assertFalse(self.p["recent"])
        self.assertIsNone(self.p["curiosity"])

    async def test_legacy_profile_without_new_fields_continues_to_learn(self):
        for key in ("recent", "curiosity", "last_curiosity"):
            self.p.pop(key, None)
        event, _ = await self.prepare("今天九点上班")
        await self.finish(event)
        await self.plugin.process(self.key, self.p)
        self.assertEqual(len(self.p["recent"]), 1)

    async def test_background_curiosity_uses_current_selected_persona(self):
        self.context.persona_manager = SimpleNamespace(get_persona_v3_by_id=lambda persona: {"prompt": "寡言，不爱打听别人的生活"})
        event, _ = await self.prepare("最近总加班")
        await self.finish(event)
        await self.plugin.process(self.key, self.p)
        payload = json.loads(self.context.llm_generate.call_args.kwargs["prompt"])
        self.assertEqual(payload["persona"], "寡言，不爱打听别人的生活")

    async def test_companion_context_never_repeats_curiosity_or_full_portrait(self):
        self.p["curiosity"] = thought(time.time())
        self.p["facts"]["private_habit"] = {"text": "爱喝无糖茶"}
        self.assertEqual(self.plugin.companion_snapshot(self.event.unified_msg_origin), {})
        self.p["routines"]["work"] = record(start="00:00", end="00:00", updated=time.time())
        value = str(self.plugin.companion_snapshot(self.event.unified_msg_origin))
        self.assertEqual(value, "{}")
        self.assertNotIn("无糖茶", value)
        self.assertNotIn("curiosity", value)

    async def test_curiosity_switch_is_honored_even_if_model_returns_a_question(self):
        self.plugin.config["curiosity_enabled"] = False
        event, _ = await self.prepare("最近总加班")
        await self.finish(event)
        candidate = thought(self.now)
        candidate["source_id"] = self.p["pending"][0]["id"]
        self.context.llm_generate.return_value.completion_text = json.dumps({"facts": [], "curiosity": candidate})
        await self.plugin.process(self.key, self.p)
        self.assertIsNone(self.p["curiosity"])

    async def test_old_questions_do_not_supply_context_for_an_unrelated_answer_days_later(self):
        self.p["recent"] = [{"id": "old", "text": "工作好累", "reply": "几点下班？", "time": self.now - 3 * 86400}]
        event, _ = await self.prepare("六点左右")
        await self.finish(event)
        await self.plugin.process(self.key, self.p)
        payload = json.loads(self.context.llm_generate.call_args.kwargs["prompt"])
        self.assertEqual(payload["recent_dialogue"], [])

    async def test_runtime_keeps_real_multimodal_text_and_excludes_internal_task_history(self):
        from astrbot.core.platform.platform_metadata import PlatformMetadata
        history = [
            {"role": "user", "content": [{"type": "text", "text": "最近总加班"}, {"type": "text", "text": "内部提示", "_no_save": True}]},
            {"role": "assistant", "content": "几点下班呀"},
            {"role": "user", "content": "<codex_task_event>任务内部报告</codex_task_event>"},
            {"role": "assistant", "content": "内部报告后的回复"},
            {"role": "user", "content": "六点左右"},
        ]
        self.context.get_platform_inst = lambda _: SimpleNamespace(meta=lambda: PlatformMetadata("test", "test", "test"))
        self.context.conversation_manager = SimpleNamespace(
            get_curr_conversation_id=AsyncMock(return_value="one"),
            get_conversation=AsyncMock(return_value=SimpleNamespace(persona_id="friend", history=json.dumps(history))))
        self.context.persona_manager = SimpleNamespace(resolve_selected_persona=AsyncMock(return_value=("friend", {"prompt": "原人格"}, None, False)))
        view = await self.plugin.runtime.view(self.p)
        self.assertEqual([r["content"] for r in view.history], ["最近总加班", "几点下班呀", "六点左右"])
        self.assertEqual(view.persona, "原人格")
