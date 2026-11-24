import json
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_daily_routine.routine import (
    active, advice, apply_updates, day_info, opportunities, parse_updates, profile_key,
)
from astrbot_plugin_daily_routine.store import Store, empty_state, new_profile


def stamp(value):
    return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()


def record(kind="work", start="09:00", end="18:00", **kw):
    return {"key": kind, "kind": kind, "start": start, "end": end, "days": list(range(7)),
            "day_type": "any", "text": kind, "updated": stamp("2026-11-24T00:00"), **kw}


class RoutineTests(unittest.TestCase):
    def setUp(self):
        self.p = new_profile("test:FriendMessage:a", "friend", "a", "Alice")
        self.config = {"china_calendar": False}

    def test_people_personas_and_groups_do_not_share_a_profile(self):
        base = profile_key("test:GroupMessage:g", "p", "a")
        self.assertNotEqual(base, profile_key("test:GroupMessage:g", "p", "b"))
        self.assertNotEqual(base, profile_key("test:GroupMessage:g", "q", "a"))
        self.assertNotEqual(base, profile_key("test:GroupMessage:h", "p", "a"))
        self.assertEqual(profile_key("wx:GroupMessage:bot|group|g|a", "p", "a"),
                         profile_key("wx:GroupMessage:bot|group|g|b", "p", "a"))

    def test_night_shift_uses_start_weekday_and_date(self):
        row = record(start="22:00", end="06:00", days=[0], date_from="2026-11-23", date_to="2026-11-23")
        self.assertTrue(active(row, datetime.fromtimestamp(stamp("2026-11-24T03:00"), ZoneInfo("Asia/Shanghai")), False))
        self.assertFalse(active(row, datetime.fromtimestamp(stamp("2026-11-24T22:30"), ZoneInfo("Asia/Shanghai")), False))
        self.assertFalse(active(row, datetime.fromtimestamp(stamp("2026-11-24T06:00"), ZoneInfo("Asia/Shanghai")), False))

    def test_sleep_blocks_only_initiative_and_work_reduces_it(self):
        self.p["routines"]["work"] = record()
        self.assertEqual(advice(self.p, stamp("2026-11-24T12:00"), self.config)["initiative_factor"], 0.15)
        self.p["routines"]["sleep"] = record("sleep", "23:00", "07:00")
        self.assertEqual(advice(self.p, stamp("2026-11-25T02:00"), self.config)["initiative_factor"], 0)
        self.assertEqual(advice(self.p, stamp("2026-11-25T19:00"), self.config)["initiative_factor"], 1)

    def test_temporary_day_off_overrides_normal_work(self):
        self.p["routines"] = {"work": record(), "leave": record("free", "09:00", "18:00", date_from="2026-11-24", date_to="2026-11-24")}
        self.assertEqual(advice(self.p, stamp("2026-11-24T12:00"), self.config)["initiative_factor"], 1)
        self.assertEqual(advice(self.p, stamp("2026-11-25T12:00"), self.config)["initiative_factor"], 0.15)

    def test_temporary_leave_does_not_offer_before_work_greeting(self):
        self.p["routines"] = {"work": record(), "leave": record("free", "09:00", "18:00", date_from="2026-11-24", date_to="2026-11-24")}
        items = opportunities(self.p, stamp("2026-11-24T08:40"), self.config)
        self.assertFalse(any(i["label"] == "上班前问候" for i in items))

    def test_lunch_break_does_not_offer_after_work_greeting(self):
        self.p["routines"] = {"morning": record(end="12:00"), "afternoon": record(start="13:00")}
        items = opportunities(self.p, stamp("2026-11-24T12:15"), self.config)
        self.assertFalse(any(i["label"] == "下班后的问候" for i in items))
        items = opportunities(self.p, stamp("2026-11-24T18:15"), self.config)
        self.assertTrue(any(i["label"] == "下班后的问候" for i in items))

    def test_unknown_and_stale_schedules_do_not_suppress(self):
        self.assertEqual(advice(self.p, stamp("2026-11-24T12:00"), self.config)["initiative_factor"], 1)
        self.p["routines"]["work"] = record(updated=0)
        self.assertEqual(advice(self.p, stamp("2026-11-24T12:00"), self.config)["initiative_factor"], 1)

    def test_night_worker_gets_wake_opportunity_after_actual_sleep(self):
        self.p["routines"]["sleep"] = record("sleep", "08:00", "16:00")
        early = opportunities(self.p, stamp("2026-11-24T08:15"), self.config)
        self.assertFalse(any(i["label"] == "早间问候" for i in early))
        later = opportunities(self.p, stamp("2026-11-24T16:15"), self.config)
        self.assertTrue(any(i["label"] == "醒来后的问候" for i in later))

    def test_bedtime_before_midnight_is_on_the_correct_day(self):
        self.p["routines"]["sleep"] = record("sleep", "00:15", "08:15")
        items = opportunities(self.p, stamp("2026-11-23T23:50"), self.config)
        # A bedtime belonging to tomorrow must still be offered tonight.
        self.assertTrue(any(i["label"] == "晚间问候" for i in items))

    def test_expired_temporary_sleep_does_not_disable_fallback(self):
        self.p["routines"]["sleep"] = record("sleep", "08:00", "16:00", date_from="2026-11-20", date_to="2026-11-20")
        self.assertTrue(any(i["label"] == "早间问候" for i in opportunities(self.p, stamp("2026-11-24T08:00"), self.config)))

    def test_offered_opportunities_are_not_repeated(self):
        now = stamp("2026-11-24T08:00")
        first = opportunities(self.p, now, self.config)
        self.assertTrue(first)
        self.p["offered"] = {i["id"]: now for i in first}
        self.assertEqual(opportunities(self.p, now + 60, self.config), [])

    def test_known_china_holiday_adjustment_and_unknown_year(self):
        self.assertEqual(day_info(date(2026, 10, 1))["holiday"], "国庆节")
        self.assertFalse(day_info(date(2026, 10, 1))["workday"])
        self.assertTrue(day_info(date(2026, 10, 10))["workday"])
        self.assertFalse(day_info(date(2040, 10, 1))["calendar_known"])
        self.assertEqual(day_info(date(2040, 10, 1))["holiday"], "")

    def test_special_date_does_not_imply_time_off(self):
        self.p["routines"]["work"] = record()
        config = {**self.config, "special_dates": ["11-24=纪念日"]}
        self.assertEqual(advice(self.p, stamp("2026-11-24T12:00"), config)["initiative_factor"], 0.15)
        self.assertTrue(any(i["context"] == "纪念日" for i in opportunities(self.p, stamp("2026-11-24T12:00"), config)))

    def test_extract_only_accepts_actual_person_evidence_and_valid_time(self):
        turns = [{"id": "one", "text": "工作日九点到六点上班"}]
        good = {**record(), "source_id": "one", "evidence": "九点到六点", "text": "九点到六点上班"}
        bad = {**good, "key": "sleep", "evidence": "晚上十一点睡"}
        invalid = {**good, "key": "invalid", "start": "25:00"}
        parsed = parse_updates(json.dumps({"routines": [good, bad, invalid]}), turns)
        self.assertEqual([r["key"] for r in parsed["routines"]], ["work"])

    def test_correction_and_removal_replace_existing_knowledge(self):
        self.p["routines"]["work"] = record()
        turns = [{"id": "two", "text": "现在改上夜班，晚上十点到早上六点"}]
        new = {**record(start="22:00", end="06:00"), "source_id": "two", "evidence": "晚上十点到早上六点"}
        updates = parse_updates(json.dumps({"routines": [new]}), turns)
        apply_updates(self.p, updates, 123)
        self.assertEqual(len(self.p["routines"]), 1)
        self.assertEqual(self.p["routines"]["work"]["start"], "22:00")
        deleted = parse_updates(json.dumps({"remove": [{"key": "work", "source_id": "two", "evidence": "改上夜班"}]}), turns)
        apply_updates(self.p, deleted, 124)
        self.assertFalse(self.p["routines"])

    def test_store_preserves_pending_and_profiles_across_reload(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            state = empty_state()
            self.p["pending"].append({"id": "one", "text": "今天加班", "time": 123})
            state["profiles"]["one"] = self.p
            store.save(state)
            self.assertEqual(store.load(), state)
            self.assertFalse(store.path.with_suffix(".tmp").exists())
