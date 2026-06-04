# PocketRole

[日本語](README.md) | English | [简体中文](README.zh-CN.md)

**A character-driven sandbox story simulation platform powered by LLMs.**

> Beta v0.1.0-beta.1 — PocketRole is under active development. APIs, configuration files, and workflows may change.
>
> PocketRole is developed Japanese-first. English and Simplified Chinese UI/documentation support is partial and experimental in v0.1.0-beta.1.

---

## What Is PocketRole?

PocketRole is a local-first sandbox simulation platform where LLM-powered characters autonomously talk, move between locations, change emotions, and generate ongoing story logs.

You define characters, places, and story rules in YAML or through the Admin Dashboard. PocketRole then runs the simulation, stores the results locally in SQLite, and can publish the generated timeline to a PHP web viewer.

```text
Your story world -> PocketRole -> autonomous character dialogue and movement
                              -> local Admin viewer
                              -> public web timeline and MAP replay
```

---

## Main Features

- Autonomous character dialogue, movement, emotions, and relationship changes
- Browser-based Admin Dashboard for stories, characters, locations, chapters, director personas, BGM, and public publishing settings
- Local Viewer with chat-style logs and character expressions
- MAP replay viewer with location backgrounds and BGM
- Multi-provider LLM support: OpenAI, Anthropic Claude, Google Gemini, DeepSeek, and local Ollama
- News Mode, director personas, chapters, beats, events, and anomalies for story progression
- Optional PHP public viewer for hosted timelines and archives

---

## Requirements

| Item | Requirement |
|---|---|
| Python | 3.11 or later |
| Database | SQLite 3 |
| Public web viewer | PHP 8.x, optional |
| LLM | OpenAI API key or local Ollama |
| OS | Linux / macOS. Windows users should use WSL. |

---

## Quick Start

```bash
git clone https://github.com/YOUR_USERNAME/pocketrole.git
cd pocketrole/pocketrole
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.yaml.example config.yaml
cp config/llm_runtime.example.yaml config/llm_runtime.local.yaml
```

Create `.env` inside the `pocketrole/` directory:

```env
OPENAI_API_KEY=sk-your-key
```

Set `active_profile: "openai_nano"` in `config/llm_runtime.local.yaml`, then run the sample story:

```bash
.venv/bin/python -m tools.validate_story stories/ankoku_gakuen/
.venv/bin/python -m tools.import_story stories/ankoku_gakuen/ --db db/pocketrole.db
.venv/bin/python -m engine.main --stories ankoku_gakuen
```

Start the Admin Dashboard in another terminal:

```bash
POCKETROLE_ADMIN_USER="admin" \
POCKETROLE_ADMIN_PASSWORD="change-me" \
POCKETROLE_ADMIN_COOKIE_SECURE=0 \
.venv/bin/python -m admin.main
```

Open:

```text
http://127.0.0.1:8787/admin
```

For the complete Japanese onboarding guide, see [START_HERE.md](START_HERE.md).

---

## Documentation

| File | Purpose |
|---|---|
| [README.md](README.md) | Japanese main README |
| [START_HERE.md](START_HERE.md) | Fastest first-run guide, Japanese |
| [AGENT_GUIDE.md](AGENT_GUIDE.md) | Canonical Japanese AI-agent operating guide |
| [AGENT_GUIDE.en.md](AGENT_GUIDE.en.md) | English summary of the AI-agent guide |
| [AGENT_GUIDE.zh-CN.md](AGENT_GUIDE.zh-CN.md) | Simplified Chinese summary of the AI-agent guide |
| [docs/specs/](docs/specs/) | Current implementation specs, mostly Japanese |
| [docs/runbooks/](docs/runbooks/) | Operational runbooks, mostly Japanese |
| [docs/users_manual/USER_MANUAL.md](docs/users_manual/USER_MANUAL.md) | User manual, Japanese |

---

## Development Status

- Version: v0.1.0-beta.1
- Branch: `upgrade-v2`
- Status: Public beta
- License: MIT

PocketRole is usable, but it is not yet a stable production framework. Please expect breaking changes during the beta period.
