from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.agent.message import TextPart

from .interop import CompanionLink
from .routine import (EXTRACT_SYSTEM, GREETING_SYSTEM, GUIDANCE, KINDS, LABELS, advice, apply_updates,
                      conversation_key, curiosity_due, human_event, minute, opportunities, parse_updates,
                      profile_key, recall, social_opening, text, actor_key, memory_rows)
from .runtime import Runtime
from .store import Store, empty_state, new_profile

NAME = "astrbot_plugin_daily_routine"
TURN = NAME + ".turn"


class DailyRoutine(Star):
    """后台了解每个人的日常，提供时机建议，让人格和情绪决定怎样相处。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.store = Store(StarTools.get_data_dir(NAME))
        self.state = empty_state()
        self.link = CompanionLink(context, config)
        self.runtime = Runtime(context, config, self.link)
        self.closing = self.sending = False
        self.worker = self.writer = self.scheduler = self.analysis_task = self.greeting_task = None
        self.save_wake, self.wake = asyncio.Event(), asyncio.Event()
        self.save_lock = asyncio.Lock()
        self.frontend = {}
        self.front_origins = {}
        self.last_front = 0
        self.analysis_provider = ""
        self.chat_providers = {}
        self.last_error = ""
        self.memory_sync_at = 0

    async def initialize(self):
        # Startup is the only read; every request thereafter uses memory.
        self.state = await asyncio.to_thread(self.store.load)
        self.link.connect()
        self.writer = asyncio.create_task(self.run_saves(), name="daily-routine-save")
        self.worker = asyncio.create_task(self.run(), name="daily-routine-learn")
        self.scheduler = asyncio.create_task(self.greetings(), name="daily-routine-greet")

    def changed(self):
        self.save_wake.set()
        self.wake.set()

    async def persist(self):
        async with self.save_lock:
            task = asyncio.create_task(asyncio.to_thread(self.store.save, copy.deepcopy(self.state)))
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise

    async def run_saves(self):
        while not self.closing:
            await self.save_wake.wait()
            self.save_wake.clear()
            await asyncio.sleep(0.2)
            try:
                await self.persist()
            except Exception as error:
                self.last_error = str(error)[:200]
                logger.warning("日常调查保存失败：%s", error)
                self.save_wake.set()
                await asyncio.sleep(5)

    def allowed(self, origin):
        if self.closing or not self.config.get("enabled", True):
            return False
        origins = self.config.get("allowed_origins", [])
        if origins and origin not in origins and conversation_key(origin) not in origins:
            return False
        try:
            enabled = self.context.get_config(umo=origin).get("plugin_set", ["*"])
            return any(key in enabled for key in ("*", NAME, getattr(self, "plugin_id", "")))
        except Exception:
            return False

    def identity(self, event):
        origin, sender = str(event.unified_msg_origin), str(event.get_sender_id() or "")
        persona = str(event.get_extra("_trace_persona_id") or "default")
        key = profile_key(origin, persona, sender)
        profile = self.state["profiles"].setdefault(key, new_profile(origin, persona, sender, text(event.get_sender_name(), 100) or sender))
        profile["origin"] = origin
        profile["name"] = text(event.get_sender_name(), 100) or sender
        return key, profile

    async def command_profile(self, event):
        if event.get_extra("_trace_persona_id") is None:
            origin = str(event.unified_msg_origin)
            manager = self.context.conversation_manager
            cid = await manager.get_curr_conversation_id(origin)
            conversation = await manager.get_conversation(origin, cid) if cid else None
            persona, _, _, _ = await self.context.persona_manager.resolve_selected_persona(
                umo=origin, conversation_persona_id=getattr(conversation, "persona_id", None),
                platform_name=event.get_platform_name(), provider_settings=self.context.get_config(umo=origin).get("provider_settings", {}),
            )
            event.set_extra("_trace_persona_id", persona)
        return self.identity(event)

    def busy(self):
        now = time.monotonic()
        self.frontend = {key: until for key, until in self.frontend.items() if until > now}
        self.front_origins = {key: origin for key, origin in self.front_origins.items() if key in self.frontend}
        return bool(self.frontend) or now - self.last_front < 3

    async def independent_provider(self):
        provider = str(self.config.get("analysis_provider_id", "") or "")
        if not provider:
            return False
        for token in self.frontend:
            origin = self.front_origins.get(token)
            if not origin:
                return False
            self.chat_providers[origin] = await self.context.get_current_chat_provider_id(origin)
            if self.chat_providers[origin] == provider:
                return False
        return True

    def selected(self, origin, persona=None, sender=None):
        return [p for p in self.state["profiles"].values()
                if conversation_key(p["origin"]) == conversation_key(origin) and not p["paused"]
                and (persona is None or p["persona"] == persona) and (sender is None or p["sender"] == str(sender))]

    @filter.on_llm_request(priority=-60)
    async def prepare(self, event: AstrMessageEvent, request: ProviderRequest):
        origin = str(event.unified_msg_origin)
        if not self.allowed(origin) or not human_event(event) or event.get_extra(TURN):
            return
        human = text(event.get_message_str(), 6000)
        if not human or human.startswith("/") or not event.get_sender_id():
            return
        key, profile = self.identity(event)
        if profile["paused"]:
            return
        now = time.time()
        mid = str(getattr(event.message_obj, "message_id", "") or uuid.uuid4().hex)
        event.set_extra(TURN, {"key": key, "generation": profile["generation"]})
        self.frontend[key + mid] = time.monotonic() + 300
        self.front_origins[key + mid] = origin
        event.get_extra(TURN)["token"] = key + mid
        self.last_front = time.monotonic()
        self.link.begin(event)
        configured = str(self.config.get("analysis_provider_id", "") or "")
        if self.analysis_task and not self.analysis_task.done():
            if not configured or self.chat_providers.get(origin) in {None, configured}:
                self.analysis_task.cancel()
        if self.greeting_task and not self.greeting_task.done() and not self.sending:
            self.greeting_task.cancel()
        previous_human = profile["last_human"]
        new_session = not previous_human or now - previous_human >= 1800
        previous_greeting = profile.get("last_message", "") if profile["last_sent"] > previous_human else ""
        profile["last_human"] = now
        profile["persona_prompt"] = text(request.system_prompt, 12000)
        profile["offered"] = {k: v for k, v in profile["offered"].items() if now - v < 3 * 86400}
        if not any(t["id"] == mid for t in [*profile.get("recent", []), *profile["pending"]]):
            profile["pending"].append({"id": mid, "text": human, "time": now, "preceding_reply": previous_greeting})
        event.get_extra(TURN)["message_id"] = mid
        self.changed()
        if not self.config.get("context_enabled", True):
            return
        # Routine awareness stays in the background unless it helps this topic.
        remembered = recall(self.memory_profile(profile), human, now, self.config)
        existing_text = "\n".join(str(getattr(p, "text", "")) for p in (request.extra_user_content_parts or []))
        remembered = [line for line in remembered if line not in existing_text]
        lines = ["以前聊过，相关时参考：" + "；".join(remembered)[:700]] if remembered else []
        if previous_greeting:
            lines.append("上次主动说过，供接住这次回应：" + previous_greeting)
        opening = new_session and social_opening(human) and len(profile["pending"]) <= 1
        thought = curiosity_due(profile, now, self.config)
        # A related new statement may already answer the old question. Let the
        # background understand it instead of re-offering that question now.
        if thought and opening:
            lines.append("以前留下的一点好奇，话题合适且你此刻也想了解时再说：" + thought["text"])
            event.get_extra(TURN)["curiosity"] = thought["key"]
        ideas = (opportunities(profile, now, self.config)
                 if opening and not thought and self.config.get("greeting_hints", True) else [])
        if ideas:
            idea = ideas[0]
            lines.append("可选时机：" + idea["label"] + "（" + idea["context"] + "）")
            event.get_extra(TURN)["offered"] = idea["id"]
        if not lines:
            return
        payload = "<daily_routine>\n" + GUIDANCE + "\n" + "\n".join(lines) + "\n</daily_routine>"
        parts = request.extra_user_content_parts
        if parts is None:
            parts = request.extra_user_content_parts = []
        if not any(str(getattr(p, "text", "")).startswith("<daily_routine>") for p in parts):
            parts.append(TextPart(text=payload).mark_as_temp())

    @filter.on_llm_response(priority=-100)
    async def response(self, event: AstrMessageEvent, response):
        turn = event.get_extra(TURN)
        if not turn or getattr(response, "tools_call_args", None):
            return
        self.frontend.pop(turn["token"], None)
        self.front_origins.pop(turn["token"], None)
        self.last_front = time.monotonic()
        self.link.end(event)
        profile = self.state["profiles"].get(turn["key"])
        if not profile or profile["generation"] != turn["generation"] or turn.get("finished"):
            return
        turn["finished"] = True
        for pending in profile["pending"]:
            if pending["id"] == turn["message_id"]:
                pending["reply"] = text(getattr(response, "completion_text", ""), 1200)
                break
        if turn.get("offered"):
            # Offered means considered, not a claim that the AI actually greeted.
            profile["offered"][turn["offered"]] = time.time()
        thought = profile.get("curiosity")
        if thought and turn.get("curiosity") == thought["key"]:
            thought["considered"] = profile["last_curiosity"] = time.time()
        self.changed()

    @filter.on_agent_done(priority=-100)
    async def agent_done(self, event: AstrMessageEvent, run_context, response):
        await self.response(event, response)

    async def process(self, key, profile):
        turns = copy.deepcopy(profile["pending"][:16])
        shared = profile.get("shared_pending")
        imported = [{"id": "cognition:" + str(r["key"]), "text": r["text"], "time": r["updated"],
                     "memory_source": "self_cognition", "original_evidence": r.get("evidence", "")}
                    for r in (shared or {}).get("records", [])]
        sources = turns + imported
        if not sources and shared is None:
            return
        generation = profile["generation"]
        zone = ZoneInfo(profile.get("timezone") or self.config.get("timezone", "Asia/Shanghai"))
        provider = str(self.config.get("analysis_provider_id", "") or "")
        self.chat_providers[profile["origin"]] = await self.context.get_current_chat_provider_id(profile["origin"])
        self.analysis_provider = provider or self.chat_providers[profile["origin"]]
        persona = profile.get("persona_prompt", "")
        if profile["persona"] == "[%None]":
            persona = ""
        elif profile["persona"] not in {"default", "_chatui_default_"}:
            manager = getattr(self.context, "persona_manager", None)
            current = manager.get_persona_v3_by_id(profile["persona"]) if manager else None
            if current:
                persona = text(current.get("prompt"), 12000)
        payload = {"person": profile["name"], "timezone": str(zone), "persona": persona,
                   "curiosity_enabled": self.config.get("curiosity_enabled", True),
                   "turns": [{**t, "at": datetime.fromtimestamp(t["time"], zone).isoformat()} for t in sources],
                   "recent_dialogue": [{**t, "at": datetime.fromtimestamp(t["time"], zone).isoformat()}
                                       for t in profile.get("recent", []) if 0 <= (turns[0]["time"] if turns else time.time()) - t["time"] < 86400],
                   "curiosity": profile.get("curiosity"),
                   "known": {field: memory_rows(self.memory_profile(profile), field) for field in ("routines", "facts")},
                   "long_term_memory": self.cognition_background(profile)}
        async with asyncio.timeout(float(self.config.get("analysis_timeout_seconds", 60))):
            response = await self.context.llm_generate(chat_provider_id=self.analysis_provider, system_prompt=EXTRACT_SYSTEM,
                                                       prompt=json.dumps(payload, ensure_ascii=False))
        updates = parse_updates(response.completion_text, sources)
        if not self.config.get("curiosity_enabled", True):
            updates["curiosity"] = None
        if (self.state["profiles"].get(key) is not profile or profile["generation"] != generation
                or profile["paused"] or not self.allowed(profile["origin"])):
            return
        native_ids = {t["id"] for t in turns}
        native = {field: [r for r in updates[field] if r["source_id"] in native_ids] for field in ("routines", "facts", "remove")}
        if "curiosity" in updates:
            native["curiosity"] = updates["curiosity"]
        apply_updates(profile, native, time.time())
        if native.get("curiosity") and native["curiosity"]["source_id"] not in native_ids:
            profile["curiosity"]["updated"] = next(t["time"] for t in imported if t["id"] == native["curiosity"]["source_id"])
        if shared is not None and profile.get("shared_pending") is shared:
            learned = {"routines": {}, "facts": {}, "exceptions": {}}
            originals = {t["id"]: t for t in imported}
            for field in ("routines", "facts"):
                for row in updates[field]:
                    original = originals.get(row["source_id"])
                    if original:
                        row = {**row, "learned_from": "self_cognition", "original_evidence": original["original_evidence"]}
                        apply_updates(learned, {"routines": [row] if field == "routines" else [],
                                               "facts": [row] if field == "facts" else [], "remove": []}, original["time"])
            profile["learned"] = learned
            profile["shared_revision"] = shared["revision"]
            profile.pop("shared_pending", None)
        profile["recent"] = [*profile.get("recent", []), *turns][-6:]
        ids = {t["id"] for t in turns}
        profile["pending"] = [t for t in profile["pending"] if t["id"] not in ids]
        profile["failures"], profile["retry_at"] = 0, 0
        self.last_error = ""
        self.changed()

    async def run(self):
        while not self.closing:
            self.wake.clear()
            self.link.connect()
            now = time.time()
            if now >= self.memory_sync_at:
                for profile in list(self.state["profiles"].values()):
                    self.sync_memory(profile)
                self.memory_sync_at = now + 30
            if not self.busy() or await self.independent_provider():
                for key, profile in list(self.state["profiles"].items()):
                    turns = profile["pending"]
                    if (not turns and "shared_pending" not in profile) or profile["paused"] or not self.allowed(profile["origin"]) or now < profile["retry_at"]:
                        continue
                    if (turns and now - turns[-1]["time"] < float(self.config.get("learn_idle_seconds", 45))
                            and len(turns) < 6 and now - turns[0]["time"] < 600):
                        continue
                    try:
                        self.analysis_task = asyncio.create_task(self.process(key, profile))
                        await self.analysis_task
                    except asyncio.CancelledError:
                        if self.closing or asyncio.current_task().cancelling():
                            raise
                    except Exception as error:
                        profile["failures"] += 1
                        profile["retry_at"] = now + min(3600, 30 * 2 ** min(profile["failures"], 7))
                        self.last_error = str(error)[:200]
                        logger.warning("日常调查后台整理稍后重试：%s", error)
                        self.changed()
                    finally:
                        self.analysis_task = None
                    break
            try:
                await asyncio.wait_for(self.wake.wait(), 5)
            except TimeoutError:
                pass

    def companion_snapshot(self, origin=""):
        # V1 snapshots are auto-injected by peers on unrelated chat requests.
        # Explicit background timing and memory interfaces own these data now.
        return {}

    def memory_profile(self, profile):
        """Read-through learned records; native and imported origins stay separate."""
        if not self.link.peer("self_cognition", profile["origin"]):
            return profile
        learned = profile.get("learned", {})
        result = {**profile}
        for field in ("routines", "facts", "exceptions"):
            result[field] = dict(learned.get(field, {}))
            for key, row in profile.get(field, {}).items():
                if row.get("updated", 0) >= result[field].get(key, {}).get("updated", 0):
                    result[field][key] = row
        for key in set(result["routines"]) & set(result["facts"]):
            older = "facts" if result["routines"][key].get("updated", 0) >= result["facts"][key].get("updated", 0) else "routines"
            result[older].pop(key)
        return result

    def cognition_background(self, profile):
        peer = self.link.peer("self_cognition", profile["origin"])
        read = getattr(peer, "companion_memory", None)
        if read:
            return read(profile["origin"], persona=profile["persona"], actor=actor_key(profile["origin"], profile["sender"]))
        if ":GroupMessage:" in profile["origin"]:
            return {}
        return self.link.snapshot("self_cognition", profile["origin"], persona=profile["persona"])

    def sync_memory(self, profile):
        if profile["paused"] or not self.allowed(profile["origin"]):
            return
        peer = self.link.peer("self_cognition", profile["origin"])
        read = getattr(peer, "companion_memory", None)
        if not read:
            return
        try:
            data = read(profile["origin"], persona=profile["persona"], actor=actor_key(profile["origin"], profile["sender"]))
        except Exception:
            return
        if not isinstance(data, dict) or data.get("version") != 1:
            return
        if (data.get("persona") != profile["persona"] or data.get("actor") != actor_key(profile["origin"], profile["sender"])
                or data.get("origin") != conversation_key(profile["origin"])):
            return
        revision = hashlib.sha256(json.dumps(data.get("records", []), ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        if revision == profile.get("shared_revision") or revision == profile.get("shared_pending", {}).get("revision"):
            return
        profile["shared_source"] = data.get("records", [])
        blocked = set(profile.get("shared_blocked", []))
        records = [row for row in data.get("records", []) if self.shared_fingerprint(row) not in blocked]
        profile["learned"] = {}
        if not records:
            profile["learned"] = {}
            profile["shared_revision"] = revision
            profile.pop("shared_pending", None)
        else:
            profile["shared_pending"] = {"revision": revision, "records": records}
        self.changed()

    @staticmethod
    def shared_fingerprint(row):
        return hashlib.sha256(json.dumps(row, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def companion_memory(self, origin, *, persona, actor):
        if not self.allowed(origin) or not self.link.peer("self_cognition", origin):
            return {}
        profiles = [p for p in self.selected(origin, persona) if actor_key(origin, p["sender"]) == actor]
        if len(profiles) != 1:
            return {}
        profile = profiles[0]
        records = [{**row, "key": field + ":" + row["key"] + ":" + row.get("date_from", "") + ":" + row.get("date_to", ""), "field": field}
                   for field in ("routines", "facts") for row in memory_rows(profile, field)]
        return {"version": 1, "origin": conversation_key(origin), "persona": persona, "actor": actor,
                "context_enabled": self.config.get("context_enabled", True), "records": records,
                "timezone": profile.get("timezone") or self.config.get("timezone", "Asia/Shanghai")}

    def companion_timing(self, origin, *, role="", sender="", persona=None):
        if not self.config.get("interop_enabled", True) or not self.allowed(origin):
            return {}
        if role and not self.link.peer(role, origin):
            return {}
        # No group-wide suppression based on one person's work schedule.
        if ":GroupMessage:" in origin and not sender:
            return {}
        profiles = self.selected(origin, persona, sender or None)
        return advice(self.memory_profile(profiles[0]), time.time(), self.config) if len(profiles) == 1 else {}

    def companion_allow_initiative(self, origin, role):
        timing = self.companion_timing(origin, role=role)
        return self.timing_choice(origin, role, timing.get("initiative_factor", 1.0))

    @staticmethod
    def timing_choice(origin, role, factor):
        # Keep the choice stable for half an hour so frequent schedulers cannot
        # repeatedly reroll a busy user's small chance until it becomes certain.
        bucket = f"{conversation_key(origin)}:{role}:{int(time.time() // 1800)}"
        draw = int.from_bytes(hashlib.sha256(bucket.encode()).digest()[:8], "big") / 2**64
        return draw < factor

    def companion_observe(self, packet):
        role, origin = packet.get("source"), packet.get("origin", "")
        if not self.allowed(origin) or not self.link.peer(role, origin):
            return
        if packet.get("kind") == "initiative":
            now = time.time()
            # Other plugins' speech is not a new human routine observation.
            for profile in self.selected(origin):
                profile["last_outbound"] = now
            self.changed()

    async def greet(self, profile):
        origin, now = profile["origin"], time.time()
        if (not self.allowed(origin) or not self.config.get("proactive_enabled", False)
                or origin not in self.config.get("proactive_origins", []) or ":FriendMessage:" not in origin
                or profile["paused"] or not profile["contact"] or profile["pending"] or "shared_pending" in profile or self.busy()):
            return
        if (not profile["last_human"] or now - profile["last_human"] > 7 * 86400
                or now - profile["last_human"] < 300 or profile["last_outbound"] > profile["last_human"]
                or now - profile["last_sent"] < float(self.config.get("greeting_gap_hours", 8)) * 3600
                or now - profile["last_checked"] < 1800):
            return
        view_profile = self.memory_profile(profile)
        ideas = opportunities(view_profile, now, self.config) if self.config.get("greeting_hints", True) else []
        thought = curiosity_due(profile, now, self.config)
        if thought:
            ideas = [{"id": "curiosity:" + thought["key"], "label": "可选的日常好奇", "context": thought["text"],
                      "reason": thought["reason"]}]
        timing = advice(view_profile, now, self.config)
        if not ideas or not self.timing_choice(origin, "daily_routine", timing["initiative_factor"]):
            return
        if self.link.snapshot("play_together", origin).get("current_game") or self.link.snapshot("self_amusement", origin).get("wants_space"):
            return
        if not await self.runtime.enabled(origin):
            return
        token = self.link.claim(float(self.config.get("interop_gap_minutes", 10)) * 60)
        if not token:
            return
        reserved_outbound = previous_outbound = None
        attempted = False
        try:
            profile["last_checked"] = now
            self.changed()
            generation = profile["generation"]
            async with asyncio.timeout(float(self.config.get("analysis_timeout_seconds", 60))):
                view = await self.runtime.view(profile)
                if view.persona_id != profile["persona"]:
                    return
                payload = {"opportunities": ideas, "routine": timing,
                           "daily_memory": recall(view_profile, "我的日常", now, self.config),
                           "long_term_memory": self.cognition_background(profile),
                           "mood": self.link.snapshot("short_term_mood", origin)}
                line = await self.runtime.draft(view, GREETING_SYSTEM, payload)
            if (not self.link.current(token) or generation != profile["generation"] or not self.allowed(origin)
                    or not self.config.get("proactive_enabled", False) or not profile["contact"] or profile["paused"]
                    or origin not in self.config.get("proactive_origins", [])):
                return
            for idea in ideas:
                if idea["id"].startswith("curiosity:"):
                    if profile.get("curiosity") and profile["curiosity"]["key"] == thought["key"]:
                        profile["curiosity"]["considered"] = profile["last_curiosity"] = now
                else:
                    profile["offered"][idea["id"]] = now
            self.changed()
            if not line or line.upper() == "WAIT":
                return
            # Persist the attempt before sending; an unconfirmed send must not be retried in a loop.
            previous_outbound = profile["last_outbound"]
            reserved_outbound = profile["last_outbound"] = time.time()
            await self.persist()
            if (not self.link.current(token) or generation != profile["generation"] or not self.allowed(origin)
                    or not self.config.get("proactive_enabled", False) or not profile["contact"] or profile["paused"]
                    or origin not in self.config.get("proactive_origins", []) or self.busy()
                    or (thought and not self.config.get("curiosity_enabled", True))):
                return
            self.sending = True
            attempted = True
            async with asyncio.timeout(30):
                delivered = await self.runtime.send(view, line[:1000])
            if delivered:
                profile["last_sent"] = time.time()
                profile["last_message"] = line[:1000]
                self.link.sent(origin, line[:1000], view.persona)
            self.changed()
        finally:
            if not attempted and reserved_outbound is not None and profile["last_outbound"] == reserved_outbound:
                profile["last_outbound"] = previous_outbound
                self.changed()
            self.sending = False
            self.link.release(token)

    async def greetings(self):
        while not self.closing:
            for profile in list(self.state["profiles"].values()):
                try:
                    self.greeting_task = asyncio.create_task(self.greet(profile))
                    await self.greeting_task
                except asyncio.CancelledError:
                    if self.closing:
                        raise
                except Exception as error:
                    self.last_error = str(error)[:200]
                    logger.warning("日常调查这次先不问候：%s", error)
                finally:
                    self.greeting_task = None
            await asyncio.sleep(60)

    @filter.command("routine", alias={"日常调查"})
    async def status(self, event: AstrMessageEvent):
        if not self.allowed(str(event.unified_msg_origin)):
            return
        _, profile = await self.command_profile(event)
        lines = [f"{profile['name']}的日常画像（当前会话与人格）", "记录：" + ("暂停" if profile["paused"] else "开启")]
        view = self.memory_profile(profile)
        for row in memory_rows(view, "routines"):
            suffix = "；截至" + row["date_to"] if row.get("date_to") else ""
            lines.append(f"[{row['key']}] {row['text']}（{row['start']}–{row['end']}{suffix}）")
        lines.extend(f"[{r['key']}] {r['text']}" for r in memory_rows(view, "facts"))
        if profile.get("curiosity"):
            lines.append("可选好奇：" + profile["curiosity"]["text"])
        lines.append("待后台整理：" + str(len(profile["pending"])) + " 条")
        if self.last_error:
            lines.append("最近后台状态：" + self.last_error)
        lines.append("修正可直接聊天；/routine_set 手动设置；/routine_forget key 删除；/routine_control pause/resume/clear；/routine_contact off/on。")
        yield event.plain_result("\n".join(lines))

    @filter.command("routine_set")
    async def set_routine(self, event: AstrMessageEvent, kind: str = "", start: str = "", end: str = "", days: str = "workday"):
        if not self.allowed(str(event.unified_msg_origin)):
            return
        try:
            if kind not in KINDS:
                raise ValueError("kind 可选 work/sleep/meal/commute/free/busy/other")
            minute(start)
            minute(end)
            if days in {"any", "workday", "restday"}:
                weekdays, day_type = list(range(7)), days
            else:
                weekdays, day_type = [int(d) for d in days.split(",")], "any"
                if not weekdays or any(not 0 <= d <= 6 for d in weekdays):
                    raise ValueError("周几使用 0–6，逗号分隔，0 是周一")
            _, profile = await self.command_profile(event)
            profile["generation"] += 1
            profile["pending"] = []
            profile["curiosity"] = None
            profile["routines"] = {k: v for k, v in profile["routines"].items() if v["kind"] != kind}
            day_label = {"workday": "工作日", "restday": "休息日", "any": "每天"}.get(days, "指定星期 " + days)
            profile["routines"]["manual_" + kind] = {"key": "manual_" + kind, "kind": kind, "start": start, "end": end,
                "days": weekdays, "day_type": day_type, "text": f"{day_label} {start}–{end} {LABELS[kind]}",
                "evidence": "本人手动设置", "source_id": "manual", "updated": time.time()}
            self.changed()
            yield event.plain_result("已记下，可用 /routine 查看。")
        except ValueError as error:
            yield event.plain_result(f"{error}。示例：/routine_set work 09:00 18:00 workday；夜班 /routine_set work 22:00 06:00 0,1,2,3,4")

    @filter.command("routine_forget")
    async def forget(self, event: AstrMessageEvent, key: str = ""):
        if not self.allowed(str(event.unified_msg_origin)):
            return
        _, profile = await self.command_profile(event)
        if not key:
            yield event.plain_result("先用 /routine 查看条目前的 key，再用 /routine_forget key 删除。")
            return
        for field in ("routines", "facts"):
            profile[field].pop(key, None)
        profile["exceptions"] = {slot: r for slot, r in profile.get("exceptions", {}).items() if r["key"] != key}
        profile["learned"] = {}
        profile["shared_blocked"] = list(set(profile.get("shared_blocked", [])) |
                                          {self.shared_fingerprint(r) for r in profile.get("shared_source", [])})
        profile.pop("shared_pending", None)
        profile["curiosity"] = None
        profile["recent"] = []
        profile["generation"] += 1
        profile["pending"] = []
        self.changed()
        yield event.plain_result("已删除该条并放下待整理片段，之后只从新的聊天重新了解。")

    @filter.command("routine_control")
    async def control(self, event: AstrMessageEvent, action: str = ""):
        if not self.allowed(str(event.unified_msg_origin)):
            return
        key, profile = await self.command_profile(event)
        if action in {"pause", "resume"}:
            profile["paused"] = action == "pause"
            profile["generation"] += 1
        elif action == "clear":
            profile["generation"] += 1
            replacement = new_profile(profile["origin"], profile["persona"], profile["sender"], profile["name"])
            replacement.update(generation=profile["generation"], contact=profile["contact"])
            replacement["shared_blocked"] = list(set(profile.get("shared_blocked", [])) |
                                                  {self.shared_fingerprint(r) for r in profile.get("shared_source", [])})
            self.state["profiles"][key] = replacement
        else:
            yield event.plain_result("/routine_control pause 暂停；resume 恢复；clear 清空自己的当前会话人格画像。")
            return
        self.changed()
        yield event.plain_result("已更新当前个人画像。")

    @filter.command("routine_contact")
    async def contact(self, event: AstrMessageEvent, mode: str = "off"):
        if not self.allowed(str(event.unified_msg_origin)):
            return
        if mode not in {"off", "on"}:
            yield event.plain_result("使用 /routine_contact off 或 on。")
            return
        _, profile = await self.command_profile(event)
        profile["contact"] = mode == "on"
        self.changed()
        yield event.plain_result("已关闭自己的日常主动联系（问候与好奇）。" if mode == "off" else "已允许日常主动联系；仍需配置中开启并选择此私聊。")

    async def terminate(self):
        self.closing = True
        tasks = [t for t in (self.worker, self.writer, self.scheduler, self.analysis_task, self.greeting_task) if t]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.persist()
        self.link.close()
