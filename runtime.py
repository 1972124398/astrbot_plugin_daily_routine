"""Use the selected AstrBot persona and mood when considering optional greetings."""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.platform import AstrBotMessage, MessageMember, MessageType
from astrbot.api.provider import ProviderRequest
from astrbot.core.platform.message_session import MessageSession


class Runtime:
    def __init__(self, context, config, link):
        self.context, self.config, self.link = context, config, link

    async def view(self, profile):
        origin = profile["origin"]
        session = MessageSession.from_str(origin)
        if session.message_type != MessageType.FRIEND_MESSAGE:
            raise ValueError("主动日常问候仅用于私聊")
        platform = self.context.get_platform_inst(session.platform_id)
        if not platform or not platform.meta().support_proactive_message:
            raise ValueError("当前平台不支持主动消息")
        manager = self.context.conversation_manager
        cid = await manager.get_curr_conversation_id(origin)
        conversation = await manager.get_conversation(origin, cid) if cid else None
        persona_id, persona, _, special = await self.context.persona_manager.resolve_selected_persona(
            umo=origin, conversation_persona_id=getattr(conversation, "persona_id", None),
            platform_name=platform.meta().name,
            provider_settings=self.context.get_config(umo=origin).get("provider_settings", {}),
        )
        prompt = str(persona.get("prompt", "")) if persona else ""
        if special:
            from astrbot.core.astr_main_agent_resources import CHATUI_SPECIAL_DEFAULT_PERSONA_PROMPT
            prompt = CHATUI_SPECIAL_DEFAULT_PERSONA_PROMPT
        message = AstrBotMessage()
        message.type, message.session_id = session.message_type, session.session_id
        message.message_id = "proactive:daily_routine:" + uuid.uuid4().hex
        message.message, message.message_str = [], ""
        message.self_id = getattr(platform, "bot_self_id", "")
        message.sender = MessageMember(profile["sender"], profile["name"])
        event = AstrMessageEvent("", message, platform.meta(), session.session_id)
        event.set_extra("daily_routine", True)
        event.set_extra("_trace_persona_id", persona_id)
        history = []
        if conversation and conversation.history:
            try:
                # Only a small amount of actual dialogue is needed for continuity.
                from .routine import text
                in_task = False
                for item in json.loads(conversation.history):
                    if not isinstance(item, dict):
                        continue
                    content = item.get("content", "")
                    if isinstance(content, list):
                        content = "\n".join(part.get("text", "") for part in content
                                            if isinstance(part, dict) and part.get("type") == "text" and not part.get("_no_save"))
                    if not isinstance(content, str):
                        continue
                    if item.get("role") == "user":
                        in_task = "<codex_task_event>" in content or "【Codex 桥接后台事件" in content
                    if not in_task and item.get("role") in {"user", "assistant"}:
                        history.append({"role": item["role"], "content": text(content, 1000)})
                history = history[-6:]
            except (ValueError, TypeError):
                pass
        if profile.get("last_message") and profile["last_sent"] > profile["last_human"]:
            history.append({"role": "assistant", "content": profile["last_message"]})
        provider = str(self.config.get("greeting_provider_id", "") or "")
        provider = provider or await self.context.get_current_chat_provider_id(origin)
        return SimpleNamespace(origin=origin, session=session, platform=platform, event=event,
                               persona=prompt, persona_id=persona_id or "default", provider=provider, history=history)

    async def draft(self, view, system, payload):
        request = ProviderRequest(system_prompt=view.persona + "\n\n" + system,
                                  prompt=json.dumps(payload, ensure_ascii=False), contexts=view.history)
        mood = self.link.peer("short_term_mood", view.origin)
        if mood:
            await mood.apply_mood(view.event, request)
        response = await self.context.llm_generate(
            chat_provider_id=view.provider, system_prompt=request.system_prompt, prompt=request.prompt,
            contexts=request.contexts, extra_user_content_parts=request.extra_user_content_parts,
        )
        return response.completion_text.strip()

    async def enabled(self, origin):
        from astrbot.core.star.session_llm_manager import SessionServiceManager
        return (await SessionServiceManager.is_session_enabled(origin)
                and await SessionServiceManager.is_llm_enabled_for_session(origin))

    async def send(self, view, message):
        chain = MessageChain().message(message)
        if view.platform.meta().name == "wechat_native":
            from ..astrbot_plugin_wechat_native.event import session_target
            target = session_target(view.session.session_id, view.platform.native_config.account_id)
            return bool(await view.platform.send_chain(target, chain))
        return await self.context.send_message(view.session, chain)
