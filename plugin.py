"""分群人格 —— 让麦麦在不同群里是不同的人。

MaiBot 的长期记忆（A_Memorix，按 chat_id）、表达方式（expression_groups 默认各群独立）
和上下文（按 stream_id）本来就已经按群隔离，唯一全局的是 ``[personality]``。
本插件只补最后一层：按群覆盖人设 / 行为风格。

实现：
- 订阅 ``maisaka.planner.before_request``：按 session_id 找到本群人格，替换 Context Items 中的 system 人设
- 订阅 ``maisaka.replyer.before_request``：同一人格以 extra_prompt 追加，保证成稿也符合本群身份

配置：插件目录下的 ``personas.toml``（不存在时读 ``personas.example.toml``）::

    [persona."1104218530"]
    name = "御姐形态"
    personality = "你叫XX，是……"
    behavior_style = "……"
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from maibot_sdk import HookHandler, MaiBotPlugin
from maibot_sdk.types import HookMode, HookOrder

PLUGIN_DIR = Path(__file__).resolve().parent
PERSONAS_FILE = PLUGIN_DIR / "personas.toml"
EXAMPLE_FILE = PLUGIN_DIR / "personas.example.toml"

log = logging.getLogger("maibot.plugin.group_persona")


def _load_personas() -> dict[str, dict[str, Any]]:
    """读取 ``[persona."<群号>"]`` 映射；文件缺失或损坏时返回空映射。"""
    path = PERSONAS_FILE if PERSONAS_FILE.exists() else EXAMPLE_FILE
    if not path.exists():
        return {}
    try:
        import tomllib

        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except Exception as exc:  # noqa: BLE001
        log.warning("读取 personas.toml 失败: %s", exc)
        return {}
    raw = data.get("persona") or {}
    out: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            out[str(key).strip()] = value
    return out


def _persona_prompt(persona: dict[str, Any]) -> str:
    """把一条人格配置拼成一段可直接放进 system 的文本。"""
    name = str(persona.get("name") or "").strip()
    personality = str(persona.get("personality") or "").strip()
    behavior = str(persona.get("behavior_style") or "").strip()
    parts = ["【本群身份】下面这段设定覆盖你的默认人设，在本群请完全按它来。"]
    if name:
        parts.append(f"你的名字/形态：{name}")
    if personality:
        parts.append(f"你的人设：{personality}")
    if behavior:
        parts.append(f"你的行为风格：{behavior}")
    return "\n".join(parts)


def _item_text(item: Any) -> str:
    """从一个 Context Item 里取出全部文本，兼容 dict / 对象两种形态。"""
    texts: list[str] = []

    def _walk(parts: Any) -> None:
        if not isinstance(parts, list):
            return
        for part in parts:
            if isinstance(part, dict):
                if part.get("type") == "text" and part.get("text"):
                    texts.append(str(part["text"]))

    if isinstance(item, dict):
        _walk(item.get("parts"))
    else:
        _walk(getattr(item, "parts", None))
    return "\n".join(texts)


def _is_system_item(item: Any) -> bool:
    if isinstance(item, dict):
        it = str(item.get("item_type") or "")
    else:
        it = str(getattr(item, "item_type", "") or "")
    return "system" in it.lower()


class GroupPersonaPlugin(MaiBotPlugin):
    def __init__(self) -> None:
        super().__init__()
        self._personas: dict[str, dict[str, Any]] = {}
        # session_id -> group_id，懒加载并在 miss 时刷新
        self._session_to_group: dict[str, str] = {}

    async def on_load(self) -> None:
        self._personas = _load_personas()
        groups = ", ".join(sorted(self._personas)) or "（无，全部走全局人格）"
        log.info("分群人格已加载，已配置群: %s", groups)
        await self._refresh_streams()

    async def on_unload(self) -> None:
        return None

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        self._personas = _load_personas()
        log.info("分群人格配置已重新加载，群数=%d", len(self._personas))

    async def _refresh_streams(self) -> None:
        """从宿主拉一次 session_id -> group_id 映射。"""
        try:
            result = await self.ctx.chat.get_all_streams()
        except Exception as exc:  # noqa: BLE001
            log.warning("获取聊天流失败（映射暂时不可用）: %s", exc)
            return
        streams = result.get("streams") if isinstance(result, dict) else result
        if not isinstance(streams, list):
            return
        mapping: dict[str, str] = {}
        for item in streams:
            if not isinstance(item, dict):
                continue
            sid = str(item.get("session_id") or item.get("stream_id") or "").strip()
            gid = str(item.get("group_id") or "").strip()
            if sid and gid:
                mapping[sid] = gid
        if mapping:
            self._session_to_group = mapping
            log.info("分群人格：已建立 %d 个会话映射 %s", len(mapping), mapping)

    async def _persona_for(self, session_id: str) -> dict[str, Any] | None:
        """按 session_id 找本群人格；找不到返回 None（走全局）。"""
        if not self._personas:
            return None
        gid = self._session_to_group.get(session_id)
        if gid is None:
            # 新群第一次出现时刷新一次
            await self._refresh_streams()
            gid = self._session_to_group.get(session_id)
        if gid is None:
            return None
        return self._personas.get(gid)

    # ---------------------------------------------------------------- planner
    @HookHandler(
        "maisaka.planner.before_request",
        name="group_persona_planner",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=3000,
    )
    async def hook_planner_before_request(self, **kwargs: Any) -> dict[str, Any] | None:
        session_id = str(kwargs.get("session_id") or "")
        persona = await self._persona_for(session_id)
        if persona is None:
            return None
        prompt = _persona_prompt(persona)
        items = kwargs.get("items")
        if not isinstance(items, list):
            return None

        replaced = False
        new_items: list[Any] = []
        for item in items:
            if not replaced and _is_system_item(item):
                # 把本群人设追加到 system 项里：只加不改，避免破坏宿主注入的其它规则
                if isinstance(item, dict):
                    parts = item.setdefault("parts", [])
                    if isinstance(parts, list):
                        parts.append({"type": "text", "text": "\n\n" + prompt})
                else:
                    try:
                        item.parts.append({"type": "text", "text": "\n\n" + prompt})
                    except Exception:  # noqa: BLE001
                        pass
                replaced = True
            new_items.append(item)

        if not replaced:
            new_items.insert(0, {
                "item_type": "SystemMessageItem",
                "parts": [{"type": "text", "text": prompt}],
            })

        log.debug("分群人格：planner 注入完成 session=%s persona=%s", session_id, persona.get("name") or "")
        kwargs["items"] = new_items
        return {"action": "continue", "modified_kwargs": kwargs}

    # ---------------------------------------------------------------- replyer
    @HookHandler(
        "maisaka.replyer.before_request",
        name="group_persona_replyer",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=3000,
    )
    async def hook_replyer_before_request(self, **kwargs: Any) -> dict[str, Any] | None:
        session_id = str(kwargs.get("session_id") or "")
        persona = await self._persona_for(session_id)
        if persona is None:
            return None
        prompt = _persona_prompt(persona) + "\n（若与系统默认人设冲突，一律以本群身份为准。）"
        log.debug("分群人格：replyer 注入 session=%s persona=%s", session_id, persona.get("name") or "")
        # extra_prompt 是追加语义：宿主会把它拼进本次 replyer 的提示词
        return {"extra_prompt": prompt}


def create_plugin() -> MaiBotPlugin:
    return GroupPersonaPlugin()
