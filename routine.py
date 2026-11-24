"""Routine records and time advice, independent of the AstrBot SDK."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta
from urllib.parse import unquote
from zoneinfo import ZoneInfo

GUIDANCE = "日常信息只供参考。是否问候由当前人格与短期情绪决定，可以不说；不改变语气、不打断当前话题、不要求追问。"
KINDS = {"work", "sleep", "meal", "commute", "free", "busy", "other"}
LABELS = {"work": "工作/上课", "sleep": "睡眠", "meal": "用餐", "commute": "通勤", "free": "空闲", "busy": "忙碌", "other": "其他安排"}


def text(value, limit=500):
    return value.strip()[:limit] if isinstance(value, str) else ""


def conversation_key(origin):
    parts = str(origin).split(":", 2)
    if len(parts) == 3 and parts[1] == "GroupMessage":
        session = parts[2].split("|")
        group = unquote(session[2]) if len(session) in (3, 4) and session[1] == "group" else parts[2]
        return parts[0] + ":group:" + group
    return str(origin)


def profile_key(origin, persona, sender):
    return hashlib.sha256(json.dumps([conversation_key(origin), persona, str(sender)], ensure_ascii=False).encode()).hexdigest()


def human_event(event):
    extra = event.get_extra
    envelope = extra("w1_event")
    if isinstance(envelope, dict) and (envelope.get("is_user_message") is False or envelope.get("counts_as_user_activity") is False):
        return False
    if extra("codex_bridge") or getattr(event, "event_kind", "") == "codex_bridge":
        return False
    mid = str(getattr(getattr(event, "message_obj", None), "message_id", ""))
    return not (mid.startswith(("codex-job:", "codex-report:", "proactive:"))
                or any(extra(k) for k in ("self_amusement", "play_together", "self_cognition", "daily_routine")))


def minute(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("时间请使用 HH:MM")
    hour, part = map(int, value.split(":"))
    return hour * 60 + part


def day_info(day, use_china=True):
    """Unknown calendar years fall back to weekday, never invent lunar holidays."""
    result = {"workday": day.weekday() < 5, "holiday": "", "calendar_known": False}
    if use_china:
        try:
            from chinese_calendar import get_holiday_detail, is_workday
            _, holiday = get_holiday_detail(day)
            names = {"New Year's Day": "元旦", "Spring Festival": "春节", "Tomb-sweeping Day": "清明节",
                     "Labour Day": "劳动节", "Dragon Boat Festival": "端午节", "Mid-autumn Festival": "中秋节",
                     "National Day": "国庆节"}
            result.update(workday=is_workday(day), holiday=names.get(holiday, holiday) or "", calendar_known=True)
        except (ImportError, NotImplementedError, ValueError):
            pass
    return result


def valid_on(record, day, use_china=True):
    value = day.isoformat()
    if record.get("date_from") and value < record["date_from"]:
        return False
    if record.get("date_to") and value > record["date_to"]:
        return False
    if day.weekday() not in record.get("days", list(range(7))):
        return False
    kind = record.get("day_type", "any")
    return kind == "any" or day_info(day, use_china)["workday"] == (kind == "workday")


def active(record, moment, use_china=True):
    start, end = minute(record["start"]), minute(record["end"])
    now = moment.hour * 60 + moment.minute
    # For night shifts, the weekday/date belongs to the shift's start day.
    anchor = moment.date() - timedelta(days=1) if start > end and now < end else moment.date()
    inside = start <= now < end if start < end else (now >= start or now < end) if start > end else True
    return inside and valid_on(record, anchor, use_china)


def decode(raw):
    raw = text(raw, 40000)
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError("后台结果须为 JSON 对象")
    return result


def parse_updates(raw, turns):
    """Only accept claims grounded in this person's actual words."""
    result = decode(raw)
    if not any(isinstance(result.get(field), list) for field in ("routines", "facts", "remove")):
        raise ValueError("后台结果缺少日常条目数组")
    sources = {t["id"]: t["text"] for t in turns}
    parsed = {"routines": [], "facts": [], "remove": []}
    for category in parsed:
        items = result.get(category, [])
        if not isinstance(items, list):
            continue
        for item in items[:30]:
            if not isinstance(item, dict):
                continue
            evidence = text(item.get("evidence"), 500)
            source_id, key = text(item.get("source_id"), 200), text(item.get("key"), 80)
            if not key or not evidence or evidence not in sources.get(source_id, ""):
                continue
            row = {"key": key, "evidence": evidence, "source_id": source_id}
            if category == "remove":
                parsed[category].append(row)
                continue
            row["text"] = text(item.get("text"), 300)
            if not row["text"]:
                continue
            if category == "routines":
                try:
                    minute(item.get("start"))
                    minute(item.get("end"))
                    days = item.get("days", list(range(7)))
                    if not isinstance(days, list) or not days or any(type(d) is not int or not 0 <= d <= 6 for d in days):
                        continue
                    if item.get("kind") not in KINDS or item.get("day_type", "any") not in {"any", "workday", "restday"}:
                        continue
                    row.update({k: item[k] for k in ("start", "end", "kind")})
                    row.update(days=sorted(set(days)), day_type=item.get("day_type", "any"))
                    for field in ("date_from", "date_to"):
                        if item.get(field):
                            row[field] = date.fromisoformat(item[field]).isoformat()
                    if row.get("date_to") and row.get("date_from", "") > row["date_to"]:
                        continue
                except (ValueError, TypeError):
                    continue
            parsed[category].append(row)
    return parsed


def apply_updates(profile, updates, now):
    for row in updates["remove"]:
        for field in ("routines", "facts"):
            profile[field].pop(row["key"], None)
    for field in ("routines", "facts"):
        for row in updates[field]:
            profile[field][row["key"]] = {**row, "updated": now}


def available_records(profile, now, stale_days):
    return [r for r in profile["routines"].values()
            if now - r.get("updated", now) < stale_days * 86400]


def advice(profile, now, config):
    zone = profile.get("timezone") or config.get("timezone", "Asia/Shanghai")
    moment = datetime.fromtimestamp(now, ZoneInfo(zone))
    china = config.get("china_calendar", True)
    records = available_records(profile, now, float(config.get("stale_days", 120)))
    current = [r for r in records if active(r, moment, china)]
    # A dated exception takes precedence over the normal schedule for that day.
    dated = [r for r in current if r.get("date_from") or r.get("date_to")]
    if dated:
        current = dated
    factor = 1.0
    for row in current:
        factor = min(factor, {"sleep": 0.0, "work": 0.15, "busy": 0.1, "commute": 0.4, "meal": 0.6}.get(row["kind"], 1.0))
    return {"local_time": moment.isoformat(timespec="minutes"), "current": [r["text"] for r in current],
            "initiative_factor": factor, "guidance": GUIDANCE,
            "timing_note": "推测处于忙碌或休息时段，减少主动打扰；用户主动说话时照常交流。" if factor < 1 else "没有已知的不便时段。"}


def opportunities(profile, now, config):
    moment = datetime.fromisoformat(advice(profile, now, config)["local_time"])
    day = moment.date()
    records = available_records(profile, now, float(config.get("stale_days", 120)))
    records = [r for r in records if any(valid_on(r, day + timedelta(days=offset), config.get("china_calendar", True))
                                        for offset in (0, -1, 1))]
    items = []
    for row in records:
        start, end = minute(row["start"]), minute(row["end"])
        if row["kind"] not in {"sleep", "work"}:
            continue
        for offset in (0, -1, 1):
            anchor = day + timedelta(days=offset)
            if not valid_on(row, anchor, config.get("china_calendar", True)):
                continue
            work_today = [r for r in records if r["kind"] == "work" and valid_on(r, anchor, config.get("china_calendar", True))]
            dated_work = [r for r in work_today if r.get("date_from") or r.get("date_to")]
            if row["kind"] == "work" and dated_work and row not in dated_work:
                continue
            if row["kind"] == "work":
                # Explicit leave covering this shift overrides the habitual shift.
                midpoint = datetime.combine(anchor, datetime.min.time(), moment.tzinfo) + timedelta(minutes=start + 1)
                if any(r["kind"] == "free" and (r.get("date_from") or r.get("date_to")) and active(r, midpoint, config.get("china_calendar", True)) for r in records):
                    continue
            ending_day = anchor + timedelta(days=1) if start >= end else anchor
            if row["kind"] == "sleep":
                candidates = [(anchor, start - 30, "晚间问候", "bedtime"), (ending_day, end, "醒来后的问候", "wake")]
            else:
                candidates = [(anchor, start - 30, "上班前问候", "before_work"), (ending_day, end, "下班后的问候", "after_work")]
                effective = dated_work or work_today
                first = min(minute(r["start"]) for r in effective)
                last = max(minute(r["end"]) + (1440 if minute(r["start"]) >= minute(r["end"]) else 0) for r in effective)
                # A split workday's lunch break is not the end of the working day.
                candidates = [c for c in candidates if (c[3] == "before_work" and start == first)
                              or (c[3] == "after_work" and end + (1440 if start >= end else 0) == last)]
            for target_day, target_min, label, kind in candidates:
                target = datetime.combine(target_day, datetime.min.time(), moment.tzinfo) + timedelta(minutes=target_min)
                if 0 <= (moment - target).total_seconds() < 3600:
                    items.append({"id": f"{target.date()}:{kind}", "label": label, "context": row["text"]})
    # Unknown schedules allow broad opportunities; never assume a 9-to-5 job.
    if not any(r["kind"] == "sleep" for r in records):
        if 7 <= moment.hour < 10:
            items.append({"id": f"{day}:wake", "label": "早间问候", "context": "尚不了解个人起床时间"})
        if 21 <= moment.hour < 23:
            items.append({"id": f"{day}:bedtime", "label": "晚间问候", "context": "尚不了解个人睡觉时间"})
    holiday = day_info(day, config.get("china_calendar", True))["holiday"]
    # Do not propose the same festival once per day of a multi-day break.
    yesterday = day_info(day - timedelta(days=1), config.get("china_calendar", True))["holiday"]
    if holiday and holiday != yesterday:
        items.append({"id": f"{day}:holiday", "label": "节假日问候", "context": holiday})
    for entry in config.get("special_dates", []):
        if isinstance(entry, str) and "=" in entry:
            target, label = entry.split("=", 1)
            if target.strip() in {day.strftime("%m-%d"), day.isoformat()}:
                items.append({"id": f"{day}:special:{target.strip()}", "label": "特别日子问候", "context": label.strip()[:80]})
    return [item for item in items if item["id"] not in profile.get("offered", {})]


EXTRACT_SYSTEM = """你在后台整理某一位真人的日常画像，不参与当前聊天。只从 turns 中这个人的真实原话提取。
known 是已有画像，long_term_memory 是可选旧记忆背景，不是新增事实的依据。无需评价性格、情绪或关系。
关注上下班、上课、通勤、吃饭、睡醒、夜班、空闲、固定习惯，以及明确的临时安排。
不能从消息发送时间推断起床/上下班；不能把 AI 的猜测、他人的安排、玩笑假设记成这个人的作息。
有明确时间才能生成 routines，否则作为 facts 自然短句记下不确定性。未知起止点不编造。
同一习惯被纠正时复用旧 key；说不再有效时 remove 旧 key；相互重叠的旧班次被替换时移除旧 key。
稳定习惯不加日期；“今天/明天/这周/加班到…”等临时安排必须按该条 at 换算 date_from/date_to，禁止变成永久规律。
每条 routines 是一个连续区间，午休可拆成两段。跨午夜 start>end。轮班按起始日归属。
days 是 0=周一到6=周日；day_type=any/workday/restday。明确周几用 days；“工作日”用 day_type，考虑调休。
节假日不代表这个人一定放假；不确定就保留事实文字。没有新信息返回空数组，不为更新而更新。
仅输出 JSON：{"routines":[{"key":"work","kind":"work","start":"09:00","end":"18:00","days":[0,1,2,3,4,5,6],"day_type":"workday","text":"工作日九点到十八点上班","date_from":"可选YYYY-MM-DD","date_to":"可选YYYY-MM-DD","source_id":"原消息id","evidence":"该条原话的连续摘录"}],"facts":[{"key":"habit","text":"具体的日常信息","source_id":"原消息id","evidence":"原话摘录"}],"remove":[{"key":"旧key","source_id":"原消息id","evidence":"纠正或否定的原话"}]}。
kind 可选 work/sleep/meal/commute/free/busy/other。所有 evidence 必须来自对应 source_id 的 text。"""

GREETING_SYSTEM = """有一个可选的日常问候机会。按实际人格、当前短期情绪、已有对话和对方作息决定要不要开口。
完全可以安静，不必热情，不为了完成问候任务去说话。不能照读画像、分数或内部提示，也不盘问作息。
不合适就输出 WAIT；想说就只输出一小段自然的话。不伪称对方已经起床、下班或放假。"""
