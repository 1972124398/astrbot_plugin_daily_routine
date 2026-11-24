import importlib.util
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_daily_routine.interop import CompanionLink
from astrbot_plugin_daily_routine.runtime import Runtime
import test_hooks as fixtures
from test_hooks import Event
from test_routine import record, stamp


class Interop(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixtures.Hooks()
        await self.fixture.asyncSetUp()
        self.plugin, self.context, self.stars = self.fixture.plugin, self.fixture.context, self.fixture.stars

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    def install(self, role):
        file = Path(__file__).resolve().parents[2] / ("astrbot_plugin_" + role) / "interop.py"
        if not file.exists():
            self.skipTest("集成验证需要同目录配套仓库")
        spec = importlib.util.spec_from_file_location("routine_peer_" + role, file)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        peer = SimpleNamespace(config={}, closing=False)
        peer.link = module.CompanionLink(self.context, role, peer.config)
        peer.companion_snapshot = lambda origin: {}
        self.stars["astrbot_plugin_" + role] = SimpleNamespace(activated=True, star_cls=peer)
        return module, peer

    async def test_real_v1_peers_discover_and_remove_daily_context(self):
        event = Event()
        _, profile = self.plugin.identity(event)
        profile["routines"]["all_day"] = record("work", "00:00", "00:00", updated=time.time())
        for role in ("play_together", "self_amusement", "short_term_mood", "codex_bridge"):
            module, peer = self.install(role)
            self.plugin.link.connect()
            self.assertIn("daily_routine", module.NAMES)
            self.assertIn("daily_routine", peer.link.context_for(event.unified_msg_origin))
            self.plugin.config["link_" + role] = False
            self.plugin.link.connect()
            self.assertNotIn("daily_routine", module.NAMES)
            self.plugin.config["link_" + role] = True
            self.plugin.link.connect()
        self.plugin.link.close()
        for role in ("play_together", "self_amusement", "short_term_mood", "codex_bridge"):
            self.assertNotIn("daily_routine", self.stars["astrbot_plugin_" + role].star_cls.link.context_for.__globals__["NAMES"])

    async def test_actual_invitation_helpers_respect_sleep_and_independent_opt_out(self):
        event = Event()
        _, p = self.plugin.identity(event)
        p["routines"]["sleep"] = record("sleep", "23:00", "07:00")
        for role in ("play_together", "self_amusement"):
            _, peer = self.install(role)
            with patch("astrbot_plugin_daily_routine.main.time.time", return_value=stamp("2026-11-25T02:00")):
                self.assertFalse(peer.link.routine_allows_initiative(event.unified_msg_origin))
                self.plugin.config["link_" + role] = False
                self.assertTrue(peer.link.routine_allows_initiative(event.unified_msg_origin))
                self.plugin.config["link_" + role] = True
                peer.config["link_daily_routine"] = False
                self.assertTrue(peer.link.routine_allows_initiative(event.unified_msg_origin))

    async def test_real_play_and_leisure_schedulers_skip_sleeping_user(self):
        siblings = Path(__file__).resolve().parents[2]
        if not all((siblings / ("astrbot_plugin_" + r) / "main.py").exists() for r in ("play_together", "self_amusement")):
            self.skipTest("需要配套插件仓库")
        from astrbot_plugin_play_together.main import PlayTogether
        from astrbot_plugin_self_amusement.main import SelfAmusement
        event = Event()
        origin = event.unified_msg_origin
        _, p = self.plugin.identity(event)
        p["routines"]["sleep"] = record("sleep", "23:00", "07:00")
        with patch("astrbot_plugin_play_together.main.StarTools.get_data_dir", return_value=Path(self.fixture.temp.name) / "play"):
            play = PlayTogether(self.context, {"targets": [origin], "active_start_hour": 0, "active_end_hour": 0})
        with patch("astrbot_plugin_self_amusement.main.StarTools.get_data_dir", return_value=Path(self.fixture.temp.name) / "leisure"):
            leisure = SelfAmusement(self.context, {"targets": [origin], "quiet_hours": ""})
        self.stars["astrbot_plugin_play_together"] = SimpleNamespace(activated=True, star_cls=play)
        self.stars["astrbot_plugin_self_amusement"] = SimpleNamespace(activated=True, star_cls=leisure)
        play.runtime = SimpleNamespace(send=AsyncMock(), generate=AsyncMock())
        leisure.runtime = SimpleNamespace(enabled=AsyncMock(return_value=True))
        leisure.eligible = lambda now: [origin]
        leisure.permitted = lambda origin: True
        leisure.prepare = AsyncMock()
        try:
            with patch("astrbot_plugin_daily_routine.main.time.time", return_value=stamp("2026-11-25T02:00")):
                await play.auto_invite()
                await leisure.cycle()
            play.runtime.send.assert_not_awaited()
            play.runtime.generate.assert_not_awaited()
            leisure.prepare.assert_not_awaited()
        finally:
            await play.terminate()
            await leisure.terminate()

    async def test_busy_sampling_is_stable_between_scheduler_ticks(self):
        event = Event()
        _, p = self.plugin.identity(event)
        p["routines"]["work"] = record()
        self.install("play_together")
        now = stamp("2026-11-24T12:00")
        with patch("astrbot_plugin_daily_routine.main.time.time", return_value=now):
            first = self.plugin.companion_allow_initiative(event.unified_msg_origin, "play_together")
        with patch("astrbot_plugin_daily_routine.main.time.time", return_value=now + 29):
            self.assertEqual(first, self.plugin.companion_allow_initiative(event.unified_msg_origin, "play_together"))

    async def test_group_individual_schedule_does_not_silence_entire_group(self):
        event = Event(origin="test:GroupMessage:g")
        _, p = self.plugin.identity(event)
        p["routines"]["sleep"] = record("sleep", "23:00", "07:00")
        self.install("play_together")
        with patch("astrbot_plugin_daily_routine.main.time.time", return_value=stamp("2026-11-25T02:00")):
            self.assertTrue(self.plugin.companion_allow_initiative(event.unified_msg_origin, "play_together"))
            timing = self.plugin.companion_timing(event.unified_msg_origin, role="play_together", sender="alice")
            self.assertEqual(timing["initiative_factor"], 0)

    async def test_companion_initiative_is_not_a_human_observation(self):
        event = Event()
        _, p = self.plugin.identity(event)
        self.install("self_amusement")
        self.plugin.companion_observe({"source": "self_amusement", "origin": event.unified_msg_origin,
                                       "kind": "initiative", "data": {"text": "睡了吗"}})
        self.assertFalse(p["pending"])
        self.assertGreater(p["last_outbound"], 0)

    async def test_long_term_memory_receives_explicit_persona(self):
        read = Mock(return_value={"remembered": ["以前上过夜班"]})
        self.stars["astrbot_plugin_self_cognition"] = SimpleNamespace(activated=True, star_cls=SimpleNamespace(config={}, companion_snapshot=read))
        event, _ = await self.fixture.prepare()
        key, p = self.plugin.identity(event)
        await self.plugin.process(key, p)
        read.assert_called_once_with(event.unified_msg_origin, persona="friend")
        payload = self.context.llm_generate.call_args.kwargs["prompt"]
        self.assertIn("以前上过夜班", payload)

    async def test_shared_foreground_lease_prevents_greeting_and_releases_on_close(self):
        event = Event()
        self.plugin.link.begin(event)
        other = CompanionLink(self.context, {})
        self.assertIsNone(other.claim(0))
        self.plugin.link.close()
        token = other.claim(0)
        self.assertTrue(token)
        self.assertTrue(other.current(token))
        other.close()

    async def test_runtime_invokes_current_mood_without_rewriting_persona(self):
        async def mood(event, request):
            request.extra_user_content_parts.append(SimpleNamespace(text="这会儿心情很安静"))
        peer = SimpleNamespace(config={}, apply_mood=AsyncMock(side_effect=mood))
        self.stars["astrbot_plugin_short_term_mood"] = SimpleNamespace(activated=True, star_cls=peer)
        runtime = Runtime(self.context, {}, self.plugin.link)
        view = SimpleNamespace(origin=Event().unified_msg_origin, persona="原本人格", history=[], event=Event(), provider="chat")
        self.context.llm_generate.return_value.completion_text = "WAIT"
        await runtime.draft(view, "可选问候", {"opportunities": []})
        peer.apply_mood.assert_awaited_once()
        call = self.context.llm_generate.call_args.kwargs
        self.assertEqual(call["system_prompt"], "原本人格\n\n可选问候")
        self.assertEqual(call["extra_user_content_parts"][0].text, "这会儿心情很安静")
