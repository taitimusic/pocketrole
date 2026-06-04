# PocketRole — AI Agent 操作指南摘要

[日本語正本](AGENT_GUIDE.md) | [English summary](AGENT_GUIDE.en.md) | 简体中文摘要

更新日：2026-06-04
目标版本：v0.1.0-beta.1

这是面向 AI 编程 Agent 的简体中文概要说明。它**不是**操作指南的正本。

权威优先级：

1. 当前代码
2. `docs/specs/`
3. `AGENT_GUIDE.md`
4. 本摘要

如果本摘要与代码、规格文档或日语版 `AGENT_GUIDE.md` 不一致，请优先遵循更高优先级的来源。

---

## PocketRole 是什么

PocketRole 是一个本地优先的沙盒故事模拟平台。它使用 LLM 让角色自动对话、移动、改变情绪，并持续生成故事日志。

生成的日志会保存到本地 SQLite。也可以发布到 PHP Web Viewer，用于公开时间线和 MAP 回放。

---

## 主要目录

- `pocketrole/engine/`：asyncio 模拟引擎、LLM 路由、故事生成、质量控制、导演和故事推进系统
- `pocketrole/admin/`：aiohttp Admin Dashboard 和管理 API
- `pocketrole/db/`：SQLite schema 和 migration
- `pocketrole/stories/`：YAML story 定义、角色图片、地点数据、BGM 相关资源
- `pocketrole/web/`：PHP 公开 viewer、receiver、timeline、archive、MAP replay
- `docs/specs/`：当前实现规格
- `docs/runbooks/`：运维步骤文档

---

## 标准安装

在 `pocketrole/` 目录中执行：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.yaml.example config.yaml
cp config/llm_runtime.example.yaml config/llm_runtime.local.yaml
```

创建 `.env` 并填写需要的 Provider key：

```env
OPENAI_API_KEY=sk-your-key
```

然后在 `config/llm_runtime.local.yaml` 中选择 active LLM profile。

---

## 常用命令

```bash
.venv/bin/python -m tools.validate_story stories/ankoku_gakuen/
.venv/bin/python -m tools.import_story stories/ankoku_gakuen/ --db db/pocketrole.db
.venv/bin/python -m engine.main --stories ankoku_gakuen
```

Admin Dashboard：

```bash
POCKETROLE_ADMIN_USER="admin" \
POCKETROLE_ADMIN_PASSWORD="change-me" \
POCKETROLE_ADMIN_COOKIE_SECURE=0 \
.venv/bin/python -m admin.main
```

测试：

```bash
.venv/bin/pytest tests/ -v --ignore=tests/test_integration.py
```

---

## AI Agent 安全原则

- 不要提交 `.env`、本地 DB、日志、API key、token 或私有自定义 story。
- 未得到用户明确许可时，不要执行破坏性的 DB / story reset。
- 优先沿用现有架构和测试，不要做大范围重写。
- LLM 相关测试应使用 `MockLLMClient` 或现有 fixture。
- 公开发布目录必须保持干净。父仓库会忽略 `pocketrole_for_github/`，该目录应通过 `tools/scripts/build_public_release.py` 生成。
- 不确定时，先阅读 `CLAUDE.md`，再阅读 `docs/specs/README.md` 和相关规格文档。

---

## 推荐交给 AI Agent 的第一条指令

```text
请帮我设置这个 PocketRole 仓库。
请先阅读 README.md、START_HERE.md、AGENT_GUIDE.md 和 docs/specs/README.md。
目标是本地运行示例 story ankoku_gakuen，并在 Admin local viewer 中确认生成的聊天日志。
未经过我确认，不要重置数据库、删除 story 数据，也不要执行 force-replace 操作。
```
