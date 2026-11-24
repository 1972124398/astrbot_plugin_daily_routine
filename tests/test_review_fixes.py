import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import unittest

import test_hooks as fixtures
from test_hooks import Event
from test_routine import record, stamp
from test_daily_memory import thought
from astrbot.api.provider import ProviderRequest
from astrbot_plugin_daily_routine.routine import advice, apply_updates, recall


class ReviewFixes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.case = fixtures.Hooks()
        await self.case.asyncSetUp()
        self.plugin = self.case.plugin
        self.now = stamp('2026-11-24T15:00')
        self.event = Event()
        self.key, self.p = self.plugin.identity(self.event)

    async def asyncTearDown(self):
        await self.case.asyncTearDown()

    async def test_temporary_shift_keeps_baseline_and_uses_it_after_expiry(self):
        self.p['routines']['work'] = record(text='平时九点到六点上班')
        override = record(start='13:00', end='22:00', text='今天换班', date_from='2026-11-24', date_to='2026-11-24')
        apply_updates(self.p, {'routines': [override], 'facts': [], 'remove': []}, self.now)
        self.assertEqual(advice(self.p, stamp('2026-11-24T10:00'), {'china_calendar': False})['initiative_factor'], 1)
        self.assertEqual(advice(self.p, stamp('2026-11-24T20:00'), {'china_calendar': False})['initiative_factor'], .15)
        self.assertEqual(advice(self.p, stamp('2026-11-25T12:00'), {'china_calendar': False})['initiative_factor'], .15)
        self.assertEqual(self.p['routines']['work']['start'], '09:00')

    async def test_dated_meal_does_not_cancel_sleep(self):
        self.p['routines'] = {'sleep': record('sleep', '08:00', '16:00'),
                              'meal': record('meal', '12:00', '12:30', date_from='2026-11-24', date_to='2026-11-24')}
        self.assertEqual(advice(self.p, stamp('2026-11-24T12:00'), {'china_calendar': False})['initiative_factor'], 0)

    async def test_explicit_answer_never_receives_the_old_question(self):
        self.p['curiosity'] = thought(self.now)
        event = Event(message='周末可以休息，已经确定了，不用再问这个')
        request = ProviderRequest(prompt=event.message)
        with patch('astrbot_plugin_daily_routine.main.time.time', return_value=self.now):
            await self.plugin.prepare(event, request)
        self.assertFalse(any('一点好奇' in p.text for p in request.extra_user_content_parts))

    async def test_work_document_task_gets_no_personal_recall_or_curiosity(self):
        self.p['curiosity'] = thought(self.now)
        self.p['routines']['work'] = record(text='工作日九点到六点上班')
        for message in ('帮我写一份加班管理制度', '请写睡眠科普文案', '帮我设计通勤路线算法'):
            event = Event(message=message)
            request = ProviderRequest(prompt=message)
            await self.plugin.prepare(event, request)
            self.assertFalse(request.extra_user_content_parts)
        self.assertTrue(recall(self.p, '根据我的作息写一份工作安排报告', self.now, {}))

    async def test_new_chat_during_persist_stops_the_autonomous_send(self):
        self.p.update(curiosity=thought(self.now), last_human=self.now - 3600)
        self.plugin.config.update(proactive_enabled=True, proactive_origins=[self.event.unified_msg_origin])
        self.plugin.runtime = SimpleNamespace(enabled=AsyncMock(return_value=True),
            view=AsyncMock(return_value=SimpleNamespace(persona_id='friend', persona='p')),
            draft=AsyncMock(return_value='周末能休息吗？'), send=AsyncMock(return_value=True))
        old = self.plugin.persist
        async def receiving():
            event = Event(message='帮我修复代码')
            await self.plugin.prepare(event, ProviderRequest(prompt=event.message))
        self.plugin.persist = receiving
        try:
            with patch('astrbot_plugin_daily_routine.main.time.time', return_value=self.now):
                await self.plugin.greet(self.p)
            self.plugin.runtime.send.assert_not_awaited()
            self.assertEqual(self.p['last_outbound'], 0)
        finally:
            self.plugin.persist = old

    async def test_separate_provider_can_start_while_other_chat_is_active(self):
        self.plugin.config['analysis_provider_id'] = 'background'
        self.p['pending'] = [{'id': 'old', 'text': '九点上班', 'time': time.time() - 1000}]
        self.plugin.frontend['other'] = time.monotonic() + 300
        self.plugin.front_origins['other'] = 'test:FriendMessage:other'
        self.plugin.process = AsyncMock()
        task = asyncio.create_task(self.plugin.run())
        try:
            for _ in range(50):
                if self.plugin.process.await_count:
                    break
                await asyncio.sleep(.01)
            self.plugin.process.assert_awaited_once()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_shared_provider_still_yields_to_foreground(self):
        self.plugin.config['analysis_provider_id'] = 'chat'
        self.p['pending'] = [{'id': 'old', 'text': '九点上班', 'time': time.time() - 1000}]
        self.plugin.frontend['other'] = time.monotonic() + 300
        self.plugin.front_origins['other'] = 'test:FriendMessage:other'
        self.plugin.process = AsyncMock()
        task = asyncio.create_task(self.plugin.run())
        await asyncio.sleep(.03)
        self.plugin.process.assert_not_awaited()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_automatic_snapshot_is_empty_even_when_frontend_is_enabled(self):
        self.p['routines']['work'] = record(start='00:00', end='00:00', updated=time.time())
        self.assertEqual(self.plugin.companion_snapshot(self.event.unified_msg_origin), {})
        self.plugin.config['context_enabled'] = False
        self.assertEqual(self.plugin.companion_snapshot(self.event.unified_msg_origin), {})

    async def test_dated_lunch_break_does_not_cancel_the_whole_work_day(self):
        self.p['routines'] = {'work': record(), 'lunch': record('free', '12:00', '13:00', date_from='2026-11-24', date_to='2026-11-24')}
        self.assertEqual(advice(self.p, stamp('2026-11-24T12:30'), {'china_calendar': False})['initiative_factor'], 1)
        self.assertEqual(advice(self.p, stamp('2026-11-24T15:00'), {'china_calendar': False})['initiative_factor'], .15)

    async def test_scheduler_cancellation_during_persist_releases_unsent_reservation(self):
        self.p.update(curiosity=thought(self.now), last_human=self.now - 3600)
        self.plugin.config.update(proactive_enabled=True, proactive_origins=[self.event.unified_msg_origin])
        self.plugin.runtime = SimpleNamespace(enabled=AsyncMock(return_value=True),
            view=AsyncMock(return_value=SimpleNamespace(persona_id='friend', persona='p')),
            draft=AsyncMock(return_value='周末能休息吗？'), send=AsyncMock(return_value=True))
        old = self.plugin.persist
        async def receiving():
            event = Event(message='帮我修复代码')
            await self.plugin.prepare(event, ProviderRequest(prompt=event.message))
            await asyncio.sleep(0)
        self.plugin.persist = receiving
        try:
            with patch('astrbot_plugin_daily_routine.main.time.time', return_value=self.now):
                self.plugin.greeting_task = asyncio.create_task(self.plugin.greet(self.p))
                await asyncio.gather(self.plugin.greeting_task, return_exceptions=True)
            self.plugin.runtime.send.assert_not_awaited()
            self.assertEqual(self.p['last_outbound'], 0)
        finally:
            self.plugin.persist = old
