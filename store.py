"""Small atomic JSON store; all I/O runs in the background."""
from __future__ import annotations

import json
import os
from pathlib import Path


def empty_state():
    return {"version": 1, "profiles": {}}


def new_profile(origin, persona, sender, name):
    return {"origin": origin, "persona": persona, "sender": str(sender), "name": name,
            "routines": {}, "facts": {}, "pending": [], "offered": {}, "paused": False,
            "contact": True, "last_human": 0, "last_sent": 0, "last_outbound": 0,
            "last_checked": 0, "generation": 0, "failures": 0, "retry_at": 0,
            "recent": [], "curiosity": None, "last_curiosity": 0}


class Store:
    def __init__(self, directory):
        self.path = Path(directory) / "routines.json"

    def load(self):
        if not self.path.exists():
            return empty_state()
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if value.get("version") != 1 or not isinstance(value.get("profiles"), dict):
            raise ValueError("不支持的日常画像数据格式")
        return value

    def save(self, state):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        os.replace(temporary, self.path)
