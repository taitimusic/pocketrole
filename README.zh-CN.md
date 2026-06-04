# PocketRole

[日本語](README.md) | [English](README.en.md) | 简体中文

**由 LLM 驱动的角色沙盒故事模拟平台。**

> Beta v0.1.0-beta.1 — PocketRole 仍在积极开发中，API、配置文件和操作流程可能会发生破坏性变更。
>
> PocketRole 目前以日语为优先开发语言。英文和简体中文的界面 / 文档支持在 v0.1.0-beta.1 阶段仍属于部分支持和实验性支持。

---

## PocketRole 是什么？

PocketRole 是一个本地优先的沙盒模拟平台。它使用 LLM 让角色自动对话、移动、改变情绪，并持续生成故事日志。

你可以通过 YAML 或 Admin Dashboard 定义角色、地点和故事规则。PocketRole 会运行模拟，将结果保存到本地 SQLite，并可以把生成的时间线发布到 PHP Web Viewer。

```text
你的世界设定 -> PocketRole -> 角色自动对话和移动
                         -> 本地 Admin Viewer
                         -> 公开 Web 时间线和 MAP 回放
```

---

## 主要功能

- 角色自动对话、移动、情绪变化和关系变化
- 通过浏览器中的 Admin Dashboard 管理故事、角色、地点、章节、导演人格、BGM 和公开发布设置
- 本地聊天式 Viewer，可显示角色表情
- MAP 回放模式，支持地点背景图和 BGM
- 支持多个 LLM Provider：OpenAI、Anthropic Claude、Google Gemini、DeepSeek、以及本地 Ollama
- News Mode、导演人格、章节、Beat、事件、异常等故事推进功能
- 可选的 PHP 公开 Viewer，用于发布公开时间线和归档

---

## 运行环境

| 项目 | 要求 |
|---|---|
| Python | 3.11 以上 |
| 数据库 | SQLite 3 |
| 公开 Web Viewer | PHP 8.x，可选 |
| LLM | OpenAI API key 或本地 Ollama |
| OS | Linux / macOS。Windows 用户建议使用 WSL。 |

---

## 快速开始

```bash
git clone https://github.com/YOUR_USERNAME/pocketrole.git
cd pocketrole/pocketrole
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.yaml.example config.yaml
cp config/llm_runtime.example.yaml config/llm_runtime.local.yaml
```

在 `pocketrole/` 目录中创建 `.env`：

```env
OPENAI_API_KEY=sk-your-key
```

在 `config/llm_runtime.local.yaml` 中把 `active_profile` 设置为 `"openai_nano"`，然后运行示例 story：

```bash
.venv/bin/python -m tools.validate_story stories/ankoku_gakuen/
.venv/bin/python -m tools.import_story stories/ankoku_gakuen/ --db db/pocketrole.db
.venv/bin/python -m engine.main --stories ankoku_gakuen
```

在另一个终端启动 Admin Dashboard：

```bash
POCKETROLE_ADMIN_USER="admin" \
POCKETROLE_ADMIN_PASSWORD="change-me" \
POCKETROLE_ADMIN_COOKIE_SECURE=0 \
.venv/bin/python -m admin.main
```

打开：

```text
http://127.0.0.1:8787/admin
```

完整的首次启动说明请参见日语文档 [START_HERE.md](START_HERE.md)。

---

## 文档

| 文件 | 用途 |
|---|---|
| [README.md](README.md) | 日语主 README |
| [START_HERE.md](START_HERE.md) | 最短首次启动指南，日语 |
| [AGENT_GUIDE.md](AGENT_GUIDE.md) | AI Agent 操作指南的日语正本 |
| [AGENT_GUIDE.en.md](AGENT_GUIDE.en.md) | AI Agent 操作指南的英文摘要 |
| [AGENT_GUIDE.zh-CN.md](AGENT_GUIDE.zh-CN.md) | AI Agent 操作指南的简体中文摘要 |
| [docs/specs/](docs/specs/) | 当前实现规格，主要为日语 |
| [docs/runbooks/](docs/runbooks/) | 运维手册，主要为日语 |
| [docs/users_manual/USER_MANUAL.md](docs/users_manual/USER_MANUAL.md) | 用户手册，日语 |

---

## 开发状态

- 版本：v0.1.0-beta.1
- 分支：`upgrade-v2`
- 状态：公开 Beta
- License：MIT

第三方组件声明请参见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

PocketRole 已经可以使用，但还不是稳定的生产级框架。Beta 阶段请预期可能出现破坏性变更。
