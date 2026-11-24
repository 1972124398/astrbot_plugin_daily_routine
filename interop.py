"""Optional companion v1 discovery and existing shared attention leases."""
from __future__ import annotations

import copy
import time
import uuid

ROLES = ("short_term_mood", "self_cognition", "play_together", "self_amusement", "codex_bridge")
ROLE = "daily_routine"
NAME = "astrbot_plugin_" + ROLE


class CompanionLink:
    def __init__(self, context, config):
        self.context, self.config = context, config
        self.owner = uuid.uuid4().hex
        state = getattr(context, "_w1_companion_v1", None)
        if not isinstance(state, dict):
            state = {"turns": {}, "revision": 0, "idle": None, "last_sent": 0, "seen": {}}
            setattr(context, "_w1_companion_v1", state)
        self.state, self.registrations = state, {}

    def peer(self, role, origin=""):
        if not all(self.config.get(k, True) for k in ("enabled", "interop_enabled", "link_" + role)):
            return None
        try:
            star = self.context.get_registered_star("astrbot_plugin_" + role)
            peer = star.star_cls if star and star.activated else None
            config = getattr(peer, "config", getattr(peer, "settings", {}))
            if not peer or getattr(peer, "closing", False) or not all(config.get(k, True) for k in ("enabled", "interop_enabled", "link_daily_routine")):
                return None
            if origin:
                allowed = self.context.get_config(umo=origin).get("plugin_set", ["*"])
                if not any(n in allowed for n in ("*", "astrbot_plugin_" + role, getattr(star, "plugin_id", ""))):
                    return None
            return peer
        except Exception:
            return None

    def snapshot(self, role, origin, persona=None):
        peer = self.peer(role, origin)
        try:
            if peer and role == "self_cognition" and persona is not None:
                result = peer.companion_snapshot(origin, persona=persona)
            else:
                result = peer.companion_snapshot(origin) if peer else {}
            return copy.deepcopy(result) if isinstance(result, dict) else {}
        except Exception:
            return {}

    def connect(self):
        # Older v1 peers already read every participant in their NAMES roster.
        for role in ROLES:
            read = getattr(getattr(self.peer(role), "link", None), "context_for", None)
            mapping = getattr(read, "__globals__", {}).get("NAMES")
            previous = self.registrations.get(role)
            if previous and previous[0] is not mapping:
                self.disconnect(role)
            if not isinstance(mapping, dict) or mapping.get(role) != "astrbot_plugin_" + role:
                self.disconnect(role)
                continue
            if role not in self.registrations:
                owned = ROLE not in mapping
                mapping.setdefault(ROLE, NAME)
                self.registrations[role] = (mapping, owned)

    def disconnect(self, role):
        previous = self.registrations.pop(role, None)
        if previous and previous[1] and previous[0].get(ROLE) == NAME:
            previous[0].pop(ROLE, None)

    def begin(self, event):
        token = event.get_extra("w1_companion_turn")
        if not isinstance(token, str):
            token = uuid.uuid4().hex
            event.set_extra("w1_companion_turn", token)
        self.state["revision"] += 1
        self.state["turns"].setdefault(token, {})[self.owner] = time.monotonic() + 300

    def end(self, event):
        self.state["turns"].get(event.get_extra("w1_companion_turn"), {}).pop(self.owner, None)

    def claim(self, gap):
        now = time.monotonic()
        state = self.state
        state["turns"] = {k: {o: end for o, end in v.items() if end > now} for k, v in state["turns"].items()}
        state["turns"] = {k: v for k, v in state["turns"].items() if v}
        if state["turns"] or time.time() - state["last_sent"] < gap:
            return None
        if state["idle"] and state["idle"]["until"] > now:
            return None
        token = uuid.uuid4().hex
        state["idle"] = {"token": token, "owner": self.owner, "revision": state["revision"], "until": now + 120}
        return token

    def current(self, token):
        lease = self.state["idle"]
        return bool(lease and lease["token"] == token and lease["until"] > time.monotonic()
                    and lease["revision"] == self.state["revision"] and not any(self.state["turns"].values()))

    def release(self, token):
        if self.state["idle"] and self.state["idle"]["token"] == token:
            self.state["idle"] = None

    def sent(self, origin, message, persona):
        now = time.time()
        self.state["last_sent"] = now
        packet = {"version": 1, "source": ROLE, "kind": "initiative", "origin": origin,
                  "id": uuid.uuid4().hex, "time": now, "data": {"text": message, "persona": persona}}
        for role in ROLES:
            receiver = getattr(self.peer(role, origin), "companion_observe", None)
            if receiver:
                try:
                    receiver(copy.deepcopy(packet))
                except Exception:
                    continue

    def close(self):
        for role in list(self.registrations):
            self.disconnect(role)
        for owners in self.state["turns"].values():
            owners.pop(self.owner, None)
        if self.state["idle"] and self.state["idle"]["owner"] == self.owner:
            self.state["idle"] = None
