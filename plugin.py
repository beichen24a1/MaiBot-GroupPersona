"""分群人格 —— 让麦麦在不同群里是不同的人。

MaiBot 的长期记忆（A_Memorix，按 chat_id）、表达方式（expression_groups 默认各群独立）
和上下文（按 stream_id）本来就已经按群隔离，唯一全局的是 ``[personality]``。
本插件只补最后一层：按群覆盖人设 / 行为风格。

实现：
- 订阅 ``maisaka.planner.before_request``：按 session_id 找到本群人格，追加到 Context Items 的 system 项
- 订阅 ``maisaka.replyer.before_request``：同一人格以 extra_prompt 追加，保证成稿也符合本群身份

配置：WebUI 插件配置页（本文件的 ``GroupPersonaConfig`` 自动生成表单）。
为了兼容旧部署，也支持插件目录下的 ``personas.toml`` —— 但 WebUI 里填了条目时以 WebUI 为准。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pydantic import Field

from maibot_sdk import HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import HookMode, HookOrder

PLUGIN_DIR = Path(__file__).resolve().parent
PERSONAS_FILE = PLUGIN_DIR / "personas.toml"
EXAMPLE_FILE = PLUGIN_DIR / "personas.example.toml"

log = logging.getLogger("maibot.plugin.group_persona")


# ------------------------------ 插件配置模型（WebUI 自动生成表单）------------------------------
class PluginSection(PluginConfigBase):
    """插件基础开关（宿主要求的 [plugin] 节）。"""

    __ui_label__ = "插件开关"
    __ui_icon__ = "toggle-left"
    __ui_order__ = 0

    enabled: bool = Field(
        default=True,
        description="关闭后插件不再改写人格，所有群都使用 bot_config.toml 的全局人格",
        json_schema_extra={"label": "启用分群人格"},
    )
    config_version: str = Field(
        default="1.0.0",
        description="配置版本，升级插件时由宿主维护，一般不需要修改",
        json_schema_extra={"label": "配置版本"},
    )


class PersonaEntry(PluginConfigBase):
    """单个群的人格设定。"""

    __ui_label__ = "群人格"

    group_id: str = Field(
        default="",
        description="QQ 群号，例如 1063132902",
        json_schema_extra={"label": "群号", "placeholder": "1063132902"},
    )
    name: str = Field(
        default="",
        description="给这套人格起的名字，方便识别，例如「大肥鱼群友」",
        json_schema_extra={"label": "人格名称", "placeholder": "大肥鱼群友"},
    )
    enabled: bool = Field(
        default=True,
        description="关掉后这个群回落到全局人格",
        json_schema_extra={"label": "启用"},
    )
    personality: str = Field(
        default="",
        description="这个群里的身份与人设：你是谁、什么形象、什么性格",
        json_schema_extra={"label": "人设", "x-widget": "textarea", "rows": 4},
    )
    behavior_style: str = Field(
        default="",
        description="行动准则：怎么说、要不要接话、爱好与态度",
        json_schema_extra={"label": "行为准则", "x-widget": "textarea", "rows": 5},
    )
    reply_style: str = Field(
        default="",
        description="说话方式：口头禅、打字习惯、禁用词（表情、客服腔等）",
        json_schema_extra={"label": "说话方式", "x-widget": "textarea", "rows": 4},
    )
    examples: str = Field(
        default="",
        description="示例对话，一行一组，例如「群友：xxx → 你：yyy」",
        json_schema_extra={"label": "示例对话", "x-widget": "textarea", "rows": 4},
    )


class GroupPersonaConfig(PluginConfigBase):
    """分群人格总配置。"""

    __ui_label__ = "分群人格"
    __ui_icon__ = "users"
    __ui_order__ = 0

    plugin: PluginSection = Field(
        default_factory=PluginSection,
        description="插件基础开关",
        json_schema_extra={"label": "插件开关"},
    )
    personas: list[PersonaEntry] = Field(
        default_factory=list,
        description="按群配置人格；没在这里出现的群继续用全局人格",
        json_schema_extra={"label": "群人格列表"},
    )


# 文件顶部用了 ``from __future__ import annotations``，嵌套模型需要显式重建
GroupPersonaConfig.model_rebuild()


# ------------------------------ 工具函数 ------------------------------
def _load_personas_from_file() -> dict[str, dict[str, Any]]:
    """兼容旧配置：从 personas.toml 读取 ``[persona."<群号>"]``。"""
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


def _entry_to_dict(entry: Any) -> dict[str, Any]:
    """把 WebUI 配置里的一条人格转成内部 dict。"""

    def _get(name: str) -> str:
        if isinstance(entry, dict):
            return str(entry.get(name) or "").strip()
        return str(getattr(entry, name, "") or "").strip()

    return {
        "name": _get("name"),
        "personality": _get("personality"),
        "behavior_style": _get("behavior_style"),
        "reply_style": _get("reply_style"),
        "examples": _get("examples"),
    }


def _persona_prompt(persona: dict[str, Any]) -> str:
    """把一条人格配置拼成一段可直接放进 system 的文本。"""
    name = str(persona.get("name") or "").strip()
    personality = str(persona.get("personality") or "").strip()
    behavior = str(persona.get("behavior_style") or "").strip()
    reply_style = str(persona.get("reply_style") or "").strip()
    examples = str(persona.get("examples") or "").strip()
    parts = ["【本群身份】下面这段设定覆盖你的默认人设，在本群请完全按它来。"]
    if name:
        parts.append(f"你的名字/形态：{name}")
    if personality:
        parts.append(f"你的人设：{personality}")
    if behavior:
        parts.append(f"你的行为准则：{behavior}")
    if reply_style:
        parts.append(f"你的说话方式：{reply_style}")
    if examples:
        parts.append(f"参考示例：\n{examples}")
    return "\n".join(parts)


def _is_system_item(item: Any) -> bool:
    if isinstance(item, dict):
        it = str(item.get("item_type") or "")
    else:
        it = str(getattr(item, "item_type", "") or "")
    return "system" in it.lower()


class GroupPersonaPlugin(MaiBotPlugin):
    config_model = GroupPersonaConfig

    def __init__(self) -> None:
        super().__init__()
        self._personas: dict[str, dict[str, Any]] = {}
        self._personas_enabled: bool = True
        # session_id -> group_id，懒加载并在 miss 时刷新
        self._session_to_group: dict[str, str] = {}

    def _reload_personas(self) -> None:
        """优先读 WebUI 配置；没有任何条目时兼容旧的 personas.toml。"""
        cfg = getattr(self, "config", None)
        plugin_section = getattr(cfg, "plugin", None) if cfg is not None else None
        if plugin_section is not None:
            self._personas_enabled = bool(getattr(plugin_section, "enabled", True))
        else:
            self._personas_enabled = bool(getattr(cfg, "enabled", True)) if cfg is not None else True
        personas: dict[str, dict[str, Any]] = {}
        entries = getattr(cfg, "personas", None) if cfg is not None else None
        if entries:
            for entry in entries:
                if not bool(getattr(entry, "enabled", True)):
                    continue
                gid = str(getattr(entry, "group_id", "") or "").strip()
                if gid:
                    personas[gid] = _entry_to_dict(entry)
        source = "WebUI"
        if not personas:
            personas = _load_personas_from_file()
            source = "personas.toml（兼容）"
        self._personas = personas
        log.info(
            "分群人格已加载（来源=%s，总开关=%s），已配置群: %s",
            source,
            "开" if self._personas_enabled else "关",
            ", ".join(sorted(personas)) or "（无，全部走全局人格）",
        )

    async def on_load(self) -> None:
        self._reload_personas()
        await self._refresh_streams()

    async def on_unload(self) -> None:
        return None

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        self._reload_personas()

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
        if not self._personas_enabled or not self._personas:
            return None
        gid = self._session_to_group.get(session_id)
        if gid is None:
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
        return {"extra_prompt": prompt}


def create_plugin() -> MaiBotPlugin:
    return GroupPersonaPlugin()
