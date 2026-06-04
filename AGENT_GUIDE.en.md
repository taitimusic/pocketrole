# PocketRole — AI Agent Guide Summary

[日本語 canonical guide](AGENT_GUIDE.md) | English summary | [简体中文摘要](AGENT_GUIDE.zh-CN.md)

Updated: 2026-06-04
Target version: v0.1.0-beta.1

This is a short English orientation for AI coding agents working on PocketRole. It is **not** the canonical operating guide.

Authoritative order:

1. Current code
2. `docs/specs/`
3. `AGENT_GUIDE.md`
4. This summary

If this summary conflicts with code, specs, or the Japanese `AGENT_GUIDE.md`, follow the higher-priority source.

---

## What PocketRole Does

PocketRole is a local-first sandbox story simulation platform. LLM-powered characters autonomously talk, move between locations, change emotions, and produce ongoing story logs.

Generated logs are stored locally in SQLite. They can also be published to a PHP web viewer for public timeline and MAP replay experiences.

---

## Key Areas

- `pocketrole/engine/`: asyncio simulation engine, LLM routing, story generation, quality control, director and progression systems
- `pocketrole/admin/`: aiohttp Admin Dashboard and APIs
- `pocketrole/db/`: SQLite schema and migrations
- `pocketrole/stories/`: YAML story definitions, character images, location data, BGM-related assets
- `pocketrole/web/`: PHP public viewer, receiver, timeline, archive, and MAP replay
- `docs/specs/`: current implementation specifications
- `docs/runbooks/`: operational procedures

---

## Standard Setup

Run commands from the `pocketrole/` directory:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.yaml.example config.yaml
cp config/llm_runtime.example.yaml config/llm_runtime.local.yaml
```

Create `.env` with the needed provider key:

```env
OPENAI_API_KEY=sk-your-key
```

Set the active LLM profile in `config/llm_runtime.local.yaml`.

---

## Common Commands

```bash
.venv/bin/python -m tools.validate_story stories/ankoku_gakuen/
.venv/bin/python -m tools.import_story stories/ankoku_gakuen/ --db db/pocketrole.db
.venv/bin/python -m engine.main --stories ankoku_gakuen
```

Admin Dashboard:

```bash
POCKETROLE_ADMIN_USER="admin" \
POCKETROLE_ADMIN_PASSWORD="change-me" \
POCKETROLE_ADMIN_COOKIE_SECURE=0 \
.venv/bin/python -m admin.main
```

Test suite:

```bash
.venv/bin/pytest tests/ -v --ignore=tests/test_integration.py
```

---

## Safety Rules For Agents

- Never commit `.env`, local DB files, logs, API keys, tokens, or private custom stories.
- Do not run destructive DB/story reset commands without explicit user approval.
- Prefer the existing architecture and tests over broad rewrites.
- Use `MockLLMClient` or existing fixtures for LLM-dependent tests.
- Keep generated public releases clean. The `pocketrole_for_github/` folder is ignored by the parent repository and should be generated with `tools/scripts/build_public_release.py`.
- When uncertain, read `CLAUDE.md`, then `docs/specs/README.md`, then the relevant spec file before editing.

---

## Recommended First Prompt To An Agent

```text
Please help me set up this PocketRole repository.
Read README.md, START_HERE.md, AGENT_GUIDE.md, and docs/specs/README.md first.
My goal is to run the sample story ankoku_gakuen locally and confirm generated chat logs in the Admin local viewer.
Do not reset databases, delete story data, or run force-replace operations without asking me first.
```
