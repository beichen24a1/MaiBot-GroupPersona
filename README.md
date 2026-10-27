# MaiBot-GroupPersona

让麦麦在**不同群里是不同的人**。

MaiBot 的长期记忆、表达方式、上下文本来就按群隔离，本插件只补上最后缺的那一层：**人格**。

## 为什么需要它

MaiBot 原生的 `[personality]` 是全局的，`maisaka_generator_base._build_personality_prompt()`
直接读 `global_config.personality.personality`，没有任何按会话覆盖的入口。
所以一旦改了人设，**所有群一起变**。

而其它维度其实早就分开了：

| 维度 | 机制 | 默认 |
| --- | --- | --- |
| 长期记忆 | A_Memorix 按 `chat_id` 检索，`shared_memory_groups` 为空时只查自己 | ✅ 按群隔离 |
| 表达方式 | `expression_groups = []` 时各群独立学习 | ✅ 按群隔离 |
| 上下文/历史 | 按 `stream_id`，每群独立会话 | ✅ 按群隔离 |
| **人格** | `global_config.personality` | ❌ **全局** |

本插件订阅两个 Hook，按 `session_id` 认出当前是哪个群，再把该群的人设注入进去：

- `maisaka.planner.before_request` —— 规划阶段，追加到 Context Items 的 system 项
- `maisaka.replyer.before_request` —— 成稿阶段，通过 `extra_prompt` 追加

## 安装

放进 MaiBot 的 `plugins/` 目录并启用：

```bash
cd MaiBot/plugins
git clone https://github.com/beichen24a1/MaiBot-GroupPersona.git
```

无额外 Python 依赖。

## 配置

把 `personas.example.toml` 复制成 `personas.toml`，按群填写（key 是 **QQ 群号**）：

```toml
[persona."1104218530"]
name = "御姐形态"
personality = "你是个二十七八岁的成熟女人，说话干脆、有主见，偶尔毒舌但不刻薄。"
behavior_style = "别人问你问题时先给结论再解释；不主动撒娇。"

[persona."1063132902"]
name = "软妹形态"
personality = "你是个刚上大学的小姑娘，说话软软的，容易害羞。"
behavior_style = "多使用短句和语气词；被夸会不好意思。"
reply_style = "常用语气：呀、啦、呜；禁止客服腔。"   # 可选：说话方式
examples = """                                       # 可选：示例对话
群友：你好可爱 → 你：诶嘿 真的吗
"""
```

- **没配置的群**继续使用 `bot_config.toml` 里的全局人格
- 改完 `personas.toml` 后在 WebUI 重载插件，或重启 core
- 插件会自己把 `session_id`（一串哈希）映射到群号，启动时拉一次，遇到新群自动刷新

## 工作方式

```
消息进来（群 1104218530）
  → Planner 请求前：session_id=407bc5ff... → group_id=1104218530 → 御姐形态
     把「【本群身份】…」追加进 system 项
  → Replyer 请求前：同一个人格，以 extra_prompt 追加
  → 模型按本群身份回复
```

日志可验证：

```
分群人格已加载，已配置群: 1063132902, 1104218530
分群人格：已建立 3 个会话映射 {'407bc5ff...': '1104218530', ...}
分群人格：planner 注入完成 session=407bc5ff... persona=御姐形态
分群人格：replyer 注入 session=407bc5ff... persona=御姐形态
```

## 已知限制

- **人格是「追加覆盖」而不是「替换」**：宿主注入的全局人设仍在 system 里，
  本插件的【本群身份】作为后置指令生效。实测能看出差别，但如果全局人设写得
  很强势，会互相掰扯 —— 建议把全局 `personality` 写得中性，群内的差异交给本插件。
  后续版本会尝试更彻底的替换（需要精确定位宿主拼进 system 的人设段落）。
- 只处理群聊；私聊仍用全局人格（要支持私聊可以按 `session_id` 加映射）。
- `personas.toml` 属于本地配置，**不入库**（仓库里只有 `.example`）。

## 目录结构

```
MaiBot-GroupPersona/
  _manifest.json
  plugin.py                两个 Hook + 配置读取 + 会话映射
  personas.example.toml    配置样例
```

## 许可

MIT
