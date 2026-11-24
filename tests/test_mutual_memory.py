"""Exercise both actual long-term memory plugins together."""
import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_hooks as fixtures
from test_hooks import Event
from test_routine import record, stamp
from astrbot_plugin_daily_routine.routine import actor_key, advice


class MutualMemory(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if not (Path(__file__).resolve().parents[2] / 'astrbot_plugin_self_cognition/main.py').exists():
            self.skipTest('Requires self cognition sibling repository')
        from astrbot_plugin_self_cognition.main import SelfCognition
        from astrbot_plugin_self_cognition.cognition import scope_key
        self.case = fixtures.Hooks()
        await self.case.asyncSetUp()
        self.daily, self.context = self.case.plugin, self.case.context
        self.context.register_web_api = lambda *args: None
        self.context.persona_manager = SimpleNamespace(get_persona_v3_by_id=lambda _: {'prompt': '原人格'})
        with patch('astrbot_plugin_self_cognition.main.StarTools.get_data_dir', return_value=Path(self.case.temp.name) / 'cognition'):
            self.cog = SelfCognition(self.context, {})
        self.case.stars['astrbot_plugin_self_cognition'] = SimpleNamespace(activated=True, star_cls=self.cog)
        self.event = Event()
        self.key, self.profile = self.daily.identity(self.event)
        self.origin = self.event.unified_msg_origin
        self.actor = actor_key(self.origin, 'alice')
        self.scope = scope_key(self.origin, 'friend')
        await self.cog.memory.call(self.cog.store.ensure_scope, self.scope, self.origin, 'friend')
        await self.cog.memory.call(self.cog.store.touch, self.scope, self.actor, 'Alice', time.time())
        await self.cog.memory.refresh(self.scope)

    async def asyncTearDown(self):
        if hasattr(self, 'cog'):
            await self.cog.terminate()
            await self.case.asyncTearDown()

    async def native_cognition(self, content='平时九点到六点上班', actor=None, updated=None):
        actor, updated = actor or self.actor, updated or time.time()
        def insert():
            with self.cog.store.db:
                self.cog.store.db.execute('INSERT INTO memories(scope,subject,slot,kind,content,evidence,source,confidence,importance,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (self.scope, actor, 'shift', 'fact', content, content, 'direct-human-turn', .9, .8, updated, updated))
        await self.cog.memory.call(insert)
        await self.cog.memory.refresh(self.scope)

    async def test_actor_identity_matches_both_plugins(self):
        from astrbot_plugin_self_cognition.cognition import actor_key as other
        self.assertEqual(self.actor, other(self.origin, 'alice'))

    async def test_daily_memory_is_learned_and_recalled_by_cognition_without_affection_gain(self):
        now = time.time()
        self.profile['facts']['meal'] = {'key': 'meal', 'text': '早餐喜欢喝牛奶', 'evidence': '我早餐喝牛奶', 'updated': now}
        state = self.cog.memory.states[self.scope]
        before = self.cog.store.stats(self.scope)['feelings']
        await self.cog.sync_daily_memory(state)
        recalled = self.cog.memory.recall(self.scope, self.actor, '早餐喝什么', conversation=True)
        imported = [r for r in recalled['memories'] if r['source'] == 'daily_routine']
        self.assertEqual(len(imported), 1)
        self.assertIn('牛奶', imported[0]['content'])
        self.assertEqual(imported[0]['updated'], now)
        self.assertEqual(self.cog.store.stats(self.scope)['feelings'], before)
        self.assertEqual(self.cog.companion_memory(self.origin, persona='friend', actor=self.actor)['records'], [])

    async def test_cognition_daily_facts_can_be_refined_into_timing_in_background(self):
        now = stamp('2026-11-24T12:00')
        await self.native_cognition(updated=now)
        self.daily.sync_memory(self.profile)
        shared = self.profile['shared_pending']['records'][0]
        self.context.llm_generate.return_value.completion_text = json.dumps({'routines': [{
            **record(text=shared['text']), 'source_id': 'cognition:' + shared['key'], 'evidence': shared['text']}], 'curiosity': None})
        await self.daily.process(self.key, self.profile)
        learned = self.daily.memory_profile(self.profile)
        self.assertEqual(advice(learned, now, {'china_calendar': False})['initiative_factor'], .15)
        self.assertEqual(self.profile['learned']['routines']['work']['updated'], now)
        self.assertEqual(self.daily.companion_memory(self.origin, persona='friend', actor=self.actor)['records'], [])
        self.daily.sync_memory(self.profile)
        self.assertNotIn('shared_pending', self.profile)

    async def test_people_and_personas_are_never_mixed(self):
        await self.native_cognition(content='小明凌晨睡觉', actor=actor_key(self.origin, 'bob'))
        self.assertEqual(self.cog.companion_memory(self.origin, persona='friend', actor=self.actor)['records'], [])
        self.assertEqual(self.cog.companion_memory(self.origin, persona='other', actor=self.actor), {})
        self.profile['facts']['tea'] = {'key': 'tea', 'text': '喜欢喝茶', 'updated': time.time()}
        self.assertEqual(self.daily.companion_memory(self.origin, persona='other', actor=self.actor), {})
        self.assertEqual(self.daily.companion_memory(self.origin, persona='friend', actor=actor_key(self.origin, 'bob')), {})

    async def test_deletion_and_link_switch_are_reflected_in_cognition(self):
        self.profile['facts']['tea'] = {'key': 'tea', 'text': '喜欢喝茶', 'updated': time.time()}
        state = self.cog.memory.states[self.scope]
        await self.cog.sync_daily_memory(state)
        self.assertTrue(self.cog.memory.recall(self.scope, self.actor, '喝茶', conversation=True)['memories'])
        self.daily.config['link_self_cognition'] = False
        self.assertFalse(self.cog.memory.recall(self.scope, self.actor, '喝茶', conversation=True)['memories'])
        self.daily.config['link_self_cognition'] = True
        self.profile['facts'].clear()
        await self.cog.sync_daily_memory(state)
        self.assertFalse(self.cog.memory.recall(self.scope, self.actor, '喝茶', conversation=True)['memories'])

    async def test_cognition_deletion_removes_learned_daily_records(self):
        await self.native_cognition()
        self.daily.sync_memory(self.profile)
        shared = self.profile['shared_pending']['records'][0]
        self.context.llm_generate.return_value.completion_text = json.dumps({'facts': [{
            'key': 'work', 'text': shared['text'], 'source_id': 'cognition:' + shared['key'], 'evidence': shared['text']}]})
        await self.daily.process(self.key, self.profile)
        self.assertTrue(self.profile['learned']['facts'])
        await self.cog.memory.call(self.cog.store.clear, self.scope)
        await self.cog.memory.refresh(self.scope)
        self.daily.sync_memory(self.profile)
        self.assertFalse(self.profile['learned'])

    async def test_frontend_opt_out_stops_shared_recall_but_keeps_background_learning(self):
        self.profile['facts']['tea'] = {'key': 'tea', 'text': '喜欢喝茶', 'updated': time.time()}
        self.daily.config['context_enabled'] = False
        await self.cog.sync_daily_memory(self.cog.memory.states[self.scope])
        self.assertFalse(self.cog.memory.recall(self.scope, self.actor, '喝茶', conversation=True)['memories'])
        self.assertTrue(self.cog.memory.recall(self.scope, self.actor, '喝茶')['memories'])

    async def test_reimport_is_idempotent_and_cognition_clear_does_not_resurrect_unchanged_snapshot(self):
        self.profile['facts']['tea'] = {'key': 'tea', 'text': '喜欢喝茶', 'updated': time.time()}
        state = self.cog.memory.states[self.scope]
        await self.cog.sync_daily_memory(state)
        await self.cog.sync_daily_memory(state)
        self.assertEqual(len(self.cog.memory.views[self.scope]['daily_memory']), 1)
        await self.cog.memory.call(self.cog.store.clear, self.scope)
        await self.cog.memory.refresh(self.scope)
        await self.cog.sync_daily_memory(self.cog.memory.states[self.scope])
        self.assertFalse(self.cog.memory.recall(self.scope, self.actor, '喝茶', conversation=True)['memories'])

    async def test_expired_imported_temporary_plan_is_not_current_recall(self):
        self.profile['exceptions'] = {'trip': {'key': 'trip', 'field': 'facts', 'text': '出差到上海',
            'date_from': '2020-01-01', 'date_to': '2020-01-01', 'updated': time.time()}}
        await self.cog.sync_daily_memory(self.cog.memory.states[self.scope])
        self.assertFalse(self.cog.memory.recall(self.scope, self.actor, '今天出差', conversation=True)['memories'])
        self.assertTrue(self.cog.memory.recall(self.scope, self.actor, '以前出差', conversation=True)['memories'])

    async def test_newer_native_daily_correction_takes_priority_over_learned_schedule(self):
        await self.native_cognition()
        self.profile['learned'] = {'routines': {'work': record(start='22:00', end='06:00', updated=time.time() - 100)}, 'facts': {}}
        self.profile['routines']['work'] = record(start='09:00', end='18:00', updated=time.time())
        self.assertEqual(self.daily.memory_profile(self.profile)['routines']['work']['start'], '09:00')

    async def test_a_changed_snapshot_does_not_resurrect_deleted_imported_records(self):
        self.profile['facts']['tea'] = {'key': 'tea', 'text': '喜欢喝茶', 'updated': time.time()}
        state = self.cog.memory.states[self.scope]
        await self.cog.sync_daily_memory(state)
        await self.cog.memory.call(self.cog.store.clear, self.scope)
        await self.cog.memory.call(self.cog.store.touch, self.scope, self.actor, 'Alice', time.time())
        await self.cog.memory.refresh(self.scope)
        self.profile['facts']['meal'] = {'key': 'meal', 'text': '早餐喝牛奶', 'updated': time.time()}
        await self.cog.sync_daily_memory(self.cog.memory.states[self.scope])
        recalled = self.cog.memory.recall(self.scope, self.actor, '', conversation=False)['memories']
        self.assertTrue(any('牛奶' in r['content'] for r in recalled))
        self.assertFalse(any('喝茶' in r['content'] for r in recalled))

    async def test_daily_clear_blocks_unchanged_cognition_records_even_if_other_records_change(self):
        await self.native_cognition()
        self.daily.sync_memory(self.profile)
        _ = [r async for r in self.daily.control(self.event, 'clear')]
        current = self.daily.state['profiles'][self.key]
        await self.native_cognition(content='晚饭常吃面条')
        self.daily.sync_memory(current)
        self.assertEqual([r['text'] for r in current['shared_pending']['records']], ['晚饭常吃面条'])

    async def test_imported_records_survive_reload_without_becoming_native_exports(self):
        self.profile['facts']['tea'] = {'key': 'tea', 'text': '喜欢喝茶', 'updated': time.time()}
        await self.cog.sync_daily_memory(self.cog.memory.states[self.scope])
        self.cog.memory.views.clear()
        await self.cog.memory.load()
        self.assertTrue(self.cog.memory.recall(self.scope, self.actor, '喝茶', conversation=True)['memories'])
        self.assertEqual(self.cog.companion_memory(self.origin, persona='friend', actor=self.actor)['records'], [])

    async def test_frontend_setting_takes_effect_before_the_next_background_sync(self):
        self.profile['facts']['tea'] = {'key': 'tea', 'text': '喜欢喝茶', 'updated': time.time()}
        await self.cog.sync_daily_memory(self.cog.memory.states[self.scope])
        self.daily.config['context_enabled'] = False
        self.assertFalse(self.cog.memory.recall(self.scope, self.actor, '喝茶', conversation=True)['memories'])
        self.assertTrue(self.cog.memory.recall(self.scope, self.actor, '喝茶')['memories'])
