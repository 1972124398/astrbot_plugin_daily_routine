"""Routine records and time advice, independent of the AstrBot SDK."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta
from urllib.parse import unquote
from zoneinfo import ZoneInfo

GUIDANCE = "这些是以前聊过的日常与可选念头，可能已有变化。按当前话题取用即可；是否问候、是否好奇由人格与短期情绪决定。不改变语气，不必提起或追问。"
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


def actor_key(origin, sender):
    return "user:" + hashlib.sha256(json.dumps([str(origin).split(":", 1)[0], str(sender)], ensure_ascii=False).encode()).hexdigest()[:24]


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
            try:
                for field in ("date_from", "date_to"):
                    if item.get(field):
                        row[field] = date.fromisoformat(item[field]).isoformat()
                if row.get("date_to") and row.get("date_from", "") > row["date_to"]:
                    continue
            except (ValueError, TypeError):
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
                except (ValueError, TypeError):
                    continue
            parsed[category].append(row)
    if result.get("curiosity", False) is None:
        parsed["curiosity"] = None
    elif isinstance(result.get("curiosity"), dict):
        item = result["curiosity"]
        evidence, source = text(item.get("evidence")), text(item.get("source_id"), 200)
        key, thought = text(item.get("key"), 80), text(item.get("text"), 180)
        if key and thought and evidence and evidence in sources.get(source, ""):
            parsed["curiosity"] = {"key": key, "text": thought, "reason": text(item.get("reason"), 200),
                                   "source_id": source, "evidence": evidence}
    return parsed


def apply_updates(profile, updates, now):
    exceptions = profile.setdefault("exceptions", {})
    for row in updates["remove"]:
        for field in ("routines", "facts"):
            profile[field].pop(row["key"], None)
        for key in list(exceptions):
            if exceptions[key]["key"] == row["key"]:
                exceptions.pop(key)
    for field in ("routines", "facts"):
        for row in updates[field]:
            key = row["key"]
            if row.get("date_from") or row.get("date_to"):
                slot = field + ":" + key + ":" + row.get("date_from", "") + ":" + row.get("date_to", "")
                exceptions[slot] = {**row, "field": field, "updated": now}
                continue
            other = "facts" if field == "routines" else "routines"
            old = profile[field].get(key) or profile[other].get(key) or {}
            history = old.get("previous", [])
            if old.get("text") and old["text"] != row["text"]:
                history = [*history, {"text": old["text"], "updated": old.get("updated", now)}][-3:]
            profile[other].pop(key, None)
            profile[field][key] = {**row, "since": old.get("since", old.get("updated", now)), "updated": now,
                                   "previous": history}
    if "curiosity" in updates:
        thought = updates["curiosity"]
        old = profile.get("curiosity") or {}
        profile["curiosity"] = ({**thought, "updated": now,
                                 "considered": old.get("considered", 0) if old.get("key") == thought["key"] else 0}
                                if thought else None)


def tokens(value):
    value = value.casefold()
    terms = set(re.findall(r"[a-z0-9_]+", value))
    for fragment in re.findall(r"[\u3400-\u9fff]+", value):
        terms.update(fragment[i:i + 2] for i in range(len(fragment) - 1))
    return terms - {"今天", "明天", "昨天", "现在", "这个", "那个", "最近", "时候", "一下", "左右", "通常", "自己", "可以", "知道", "我的", "你的", "还是"}


TOPICS = (
    ("work", "上班|下班|工作|加班|上课|放学|夜班|值班|轮班|调休|放假|休假"),
    ("sleep", "睡觉|睡眠|起床|醒|熬夜|晚安|早安|几点睡|几点起"),
    ("meal", "吃饭|早餐|早饭|午饭|午餐|晚饭|晚餐|吃什么|饮食|做饭"),
    ("commute", "通勤|地铁|公交|开车|堵车|坐车|上下班路"),
    ("free", "有空|空闲|周末|休息|下班后|闲时|平时|日常|习惯"),
)


def relevance(query, row):
    """Small local topic recall; unrelated memory gets a zero score."""
    content = row.get("text", "") + " " + row.get("reason", "") + " " + row.get("evidence", "")
    shared = len(tokens(query) & tokens(content))
    categories = sum(bool(re.search(pattern, query)) and (row.get("kind") == kind or bool(re.search(pattern, content)))
                     for kind, pattern in TOPICS)
    return shared + categories * 3


def personal_topic(query):
    """Explicit personal recall remains available even inside a work request."""
    if re.search(r"根据我|按我|我的作息|我的日常|我的习惯|记得我", query):
        return True
    return not (re.search(r"帮|请|给我|写一|生成|分析|解释|修复|翻译|总结|设计|编写|科普", query)
                and re.search(r"代码|程序|制度|文案|报告|工作流|函数|算法|作文|论文|科普|模板|总结|管理办法", query))


def memory_rows(profile, field):
    return [*profile.get(field, {}).values(),
            *(r for r in profile.get("exceptions", {}).values() if r.get("field") == field)]


def recall(profile, query, now, config, limit=3):
    """Recall relevant facts, keeping expired plans out of present-day context."""
    if not personal_topic(query):
        return []
    local_day = datetime.fromtimestamp(now, ZoneInfo(profile.get("timezone") or config.get("timezone", "Asia/Shanghai"))).date()
    offset = next((offset for word, offset in (("后天", 2), ("明天", 1), ("昨天", -1), ("前天", -2)) if word in query), 0)
    day = (local_day + timedelta(days=offset)).isoformat()
    historical = bool(re.search(r"以前|之前|过去|原来|曾经", query))
    broad = bool(re.search(r"我的作息|我的日常|我的习惯|记得.*作息", query))
    candidates = []
    for field in ("routines", "facts"):
        rows = memory_rows(profile, field)
        if field == "routines" and not historical:
            rows = effective_records(rows, datetime.combine(local_day + timedelta(days=offset), datetime.min.time(), ZoneInfo(profile.get("timezone") or config.get("timezone", "Asia/Shanghai"))), config.get("china_calendar", True))
        for row in rows:
            current = not ((row.get("date_to") and row["date_to"] < day) or (row.get("date_from") and row["date_from"] > day))
            score = relevance(query, row)
            if not (score or broad) or (not current and not historical):
                continue
            value = row["text"]
            if row.get("date_from") or row.get("date_to"):
                period = row.get("date_from", "") + (" 至 " + row["date_to"] if row.get("date_to") != row.get("date_from") and row.get("date_to") else "")
                value = period.strip() + "的安排：" + value
            if not current:
                value = "过去或其他日期的安排：" + value
            elif now - row.get("updated", now) >= float(config.get("stale_days", 120)) * 86400:
                value = "以前提过，近期未确认：" + value
            if historical and row.get("previous"):
                value += "（更早提过：" + row["previous"][-1]["text"] + "）"
            candidates.append((score, row.get("updated", 0), value))
    return list(dict.fromkeys(value for _, _, value in sorted(candidates, reverse=True)))[:limit]


def curiosity_due(profile, now, config):
    thought = profile.get("curiosity")
    if (not config.get("curiosity_enabled", True) or not thought
            or now - thought.get("updated", 0) > 14 * 86400
            or now - max(thought.get("considered", 0), profile.get("last_curiosity", 0)) < 3 * 86400):
        return None
    return thought


def social_opening(query):
    return bool(re.fullmatch(r"(?:早[呀啊安上好]*|晚上好|晚安|你好[呀啊]*|嗨|哈喽|在吗|在不在|我[又回]来[了啦]?|好久不见|回来[啦了]|聊聊|聊会天|今天好累)[，。！？!,.?～~ ]*", query))


def available_records(profile, now, stale_days):
    return [r for r in memory_rows(profile, "routines")
            if now - r.get("updated", now) < stale_days * 86400]


def effective_records(records, moment, china):
    """Dated shifts replace their own kind for the day, not unrelated sleep."""
    base = [r for r in records if not (r.get("date_from") or r.get("date_to"))]
    dated = []
    anchors = {}
    for row in records:
        if not (row.get("date_from") or row.get("date_to")):
            continue
        anchor = moment.date()
        if active(row, moment, china) and minute(row["start"]) > minute(row["end"]) and moment.hour * 60 + moment.minute < minute(row["end"]):
            anchor -= timedelta(days=1)
        if not valid_on(row, anchor, china):
            continue
        if any(b.get("key") == row.get("key") and b.get("kind") == row.get("kind")
               and valid_on(b, anchor, china) and b.get("updated", 0) > row.get("updated", 0) for b in base):
            continue
        dated.append(row)
        anchors[id(row)] = anchor
    return [*dated, *(r for r in base if not any(
        ((r.get("kind") == d.get("kind") and valid_on(r, anchors[id(d)], china))
         or (d.get("kind") == "free" and active(d, moment, china) and r.get("kind") in {"work", "busy"}))
        for d in dated))]


def advice(profile, now, config):
    zone = profile.get("timezone") or config.get("timezone", "Asia/Shanghai")
    moment = datetime.fromtimestamp(now, ZoneInfo(zone))
    china = config.get("china_calendar", True)
    records = available_records(profile, now, float(config.get("stale_days", 120)))
    current = [r for r in effective_records(records, moment, china) if active(r, moment, china)]
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


EXTRACT_SYSTEM = """你在后台整理某一位真人的日常长期记忆，并按 persona 的实际人格决定有无一点自然的好奇，不参与当前聊天。
known 是已有画像，long_term_memory 是可选旧记忆背景。recent_dialogue 和 turns 中 reply/preceding_reply 只是语境，不能当真人新事实。
turns 中 memory_source=self_cognition 的条目是长期记忆插件直接形成的本人记忆，允许整理其中的日常，但必须保留原日期、用该条 source_id/id 为依据。它不是新的真人发言，不代表此刻活动或新确认；没有明确新变化时保留原有记录 key。
只从 turns 中这个人的 text 提取新事实；可结合前面的实际问答理解省略回答。例如刚问几点下班，对方说“六点左右”，可以记通常六点下班，不能编上班时间。
关注上下班、上课、通勤、吃饭、睡醒、夜班、空闲、平时活动、生活偏好与固定习惯，以及明确的临时安排。无需评价性格、情绪或关系。
不能从消息发送时间推断起床/上下班；不能把 AI 的猜测、他人的安排、玩笑假设记成这个人的作息。
“以前上夜班、现在不干了”等过去的生活保留为标明过去的 facts，不生成现行作息区间。
有明确时间才能生成 routines，否则作为 facts 自然短句记下不确定性。未知起止点不编造。
同一习惯被纠正时复用旧 key；说不再有效时 remove 旧 key；相互重叠的旧班次被替换时移除旧 key。
稳定习惯不加日期；“今天/明天/这周/加班到…”等临时安排无论作为 routines 还是 facts，都必须按该条 at 换算 date_from/date_to，禁止变成永久规律。
文字中的“今天/明天”尽量换成具体日期，避免之后回想时把旧的相对日期当成现在。
每条 routines 是一个连续区间，午休可拆成两段。跨午夜 start>end。轮班按起始日归属。
days 是 0=周一到6=周日；day_type=any/workday/restday。明确周几用 days；“工作日”用 day_type，考虑调休。
节假日不代表这个人一定放假；不确定就保留事实文字。没有新信息返回空数组，不为更新而更新。
curiosity_enabled 为 false 时不产生好奇，curiosity=null。否则可以留下至多一个 curiosity：因这段真实交流而想继续了解的日常小事，例如对方说最近常加班，想知道哪天终于有空休息。
必须与此人刚说过的具体生活有关，以 source_id/evidence 为缘由；按 persona 决定是否真的感兴趣，可以完全没有。不是补齐画像的任务，不列缺失字段，不套固定问题。
对方不想谈、问题已经有答案、念头已不合时宜或没有值得继续了解的地方时 curiosity=null。已有念头还合适时可保留同一 key；换种说法不算新问题。
不用承诺“下次一定问”，不设计语气、亲密程度或情绪。curiosity.text 用一句自然念头表述，具体是否问、何时问交给前台人格。
仅输出 JSON：{"routines":[{"key":"work","kind":"work","start":"09:00","end":"18:00","days":[0,1,2,3,4,5,6],"day_type":"workday","text":"工作日九点到十八点上班","date_from":"可选YYYY-MM-DD","date_to":"可选YYYY-MM-DD","source_id":"原消息id","evidence":"该条原话的连续摘录"}],"facts":[{"key":"habit","text":"具体的日常信息","source_id":"原消息id","evidence":"原话摘录"}],"remove":[{"key":"旧key","source_id":"原消息id","evidence":"纠正或否定的原话"}]}。
facts 同样可以带可选的 date_from/date_to。可再加 "curiosity":{"key":"overtime_rest","text":"最近总加班，不知道这周能不能歇一歇","reason":"对方提到最近连续加班","source_id":"原消息id","evidence":"连续加班的原话"}，没有则为 null。
kind 可选 work/sleep/meal/commute/free/busy/other。所有 evidence 必须来自对应 source_id 的 text。
已记住的同一件事沿用 key；从不完整事实补成有明确起止时间的区间也沿用 key，不同时留下相互矛盾的两份当前认识。
当前问题的回答进入新记忆后，放下已解答的 curiosity，别重复问；闲聊里没有新的日常信息可以什么也不记。"""

GREETING_SYSTEM = """后台留下一个可选的日常念头，可能是问候，也可能是对以前聊过的小事有点好奇。
按实际人格、当前短期情绪、已有对话和对方作息决定要不要开口；即使机会存在也完全可以安静。
日常记忆只帮助理解生活，不规定表达风格、亲密程度或关系。不要为了了解日常去盘问、填表、连续追问。
先看近期问答，已经说过、已经有答案、对方不想谈就放下。不必热情，不为完成问候或收集信息任务去说话。
若这时确实想了解，围绕一件事随口说一句即可，可以提起近况而不问问题。不能照读画像、分数或内部提示。
不合适就输出 WAIT；想说就只输出一小段自然的话。不伪称对方已经起床、下班或放假。"""
