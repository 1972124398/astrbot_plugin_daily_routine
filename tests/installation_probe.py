"""Real AstrBot archive install, initialize, configure and hot reload."""
import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

from astrbot.core import LogBroker, db_helper
from astrbot.core.core_lifecycle import AstrBotCoreLifecycle


async def verify(source):
    root = Path(os.environ["ASTRBOT_ROOT"])
    sys.path.insert(0, str(root))
    core = AstrBotCoreLifecycle(LogBroker(), db_helper)
    with patch("astrbot.core.core_lifecycle.update_llm_metadata", AsyncMock()):
        await core.initialize()
    name = "astrbot_plugin_daily_routine"
    try:
        await core.plugin_manager.install_plugin_from_file(source)
        metadata = core.star_context.get_registered_star(name)
        assert metadata and metadata.activated and metadata.star_cls
        assert metadata.config["proactive_enabled"] is False
        assert metadata.config["proactive_origins"] == []
        assert metadata.config.schema["allowed_origins"]["_special"] == "umo"
        instance = metadata.star_cls
        origin = "probe:FriendMessage:alice"
        event = type("Event", (), {
            "unified_msg_origin": origin, "get_sender_id": lambda _: "alice",
            "get_sender_name": lambda _: "Alice", "get_extra": lambda _, key: "friend" if key == "_trace_persona_id" else None,
        })()
        key, profile = instance.identity(event)
        profile["facts"]["shift"] = {"key": "shift", "text": "夜班", "updated": 100}
        await instance.persist()
        metadata.config.save_config({"allowed_origins": [origin]})
        success, error = await core.plugin_manager.reload(name)
        assert success, error
        current = core.star_context.get_registered_star(name)
        assert current.star_cls is not instance
        assert current.config["allowed_origins"] == [origin]
        assert current.star_cls.state["profiles"][key]["facts"]["shift"]["text"] == "夜班"
        assert current.star_cls.worker and not current.star_cls.worker.done()
    finally:
        await core.stop()


if __name__ == "__main__":
    asyncio.run(verify(sys.argv[1]))
