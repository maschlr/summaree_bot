# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AI-powered Telegram bot for transcribing, summarizing, and translating voice messages and audio files. Production bot: [@summaree_bot](https://t.me/summaree_bot)

Tech stack: Python 3.12+, python-telegram-bot, SQLAlchemy+PostgreSQL, OpenAI (transcription/summarization), DeepL (translation), Alembic (migrations)

## Commands

```bash
# Run the bot (supports webhook or polling mode based on TELEGRAM_WEBHOOK_URL env var)
python bot.py

# Run tests (requires TEST_DB_URL env var)
pytest summaree_bot/tests/

# Run single test file
pytest summaree_bot/tests/test_models.py

# Database migrations
alembic revision -m "message" --autogenerate
alembic upgrade head

# Initialize database (create tables)
python -m scripts.create_database

# Linting/formatting (via pre-commit)
pre-commit run --all-files
```

## Code Quality

- Line length: 120 chars
- Linters: Black, isort (black profile), Ruff (E/F/B rules, max-complexity 10)
- Pre-commit hooks configured in `.pre-commit-config.yaml`

## Architecture

### Entry Point & Bot Setup (`bot.py`)
- Configures Telegram handlers (commands, messages, callbacks, payments)
- Supports webhook mode (production) and polling mode (development)
- Job queue runs subscription status updates (30min) and message queue processing (1min)

### Core Patterns

**Decorator-based Session Management** (`summaree_bot/bot/db.py`):
- `@session_context`: Wraps handler with SQLAlchemy session lifecycle
- `@ensure_chat`: Creates/updates User and Chat records on every request

**Handler Chain**: Command handlers → Message handlers (voice/audio/video) → Callback handlers → Error handlers

### Module Structure

- `summaree_bot/bot/` - Telegram handlers: `common.py` (transcription flow), `user.py` (user commands), `admin.py` (admin commands), `premium.py` (payments)
- `summaree_bot/models/` - SQLAlchemy ORM: TelegramUser, TelegramChat, Transcript, Summary, Language, Subscription, Invoice
- `summaree_bot/integrations/` - External APIs: `openai.py`, `deepl.py`, `audio.py`
- `summaree_bot/templates/` - Jinja2 templates for bot responses

### Environment Variables

Required (see `summaree_bot/.env.example`):
- `TELEGRAM_BOT_TOKEN`, `ADMIN_CHAT_ID`, `OPENAI_API_KEY`, `DEEPL_TOKEN`, `DB_URL`

Optional:
- `TELEGRAM_WEBHOOK_URL`, `TELEGRAM_WEBHOOK_SECRET_TOKEN` (for webhook mode)
- `STRIPE_TOKEN`, `PAYMENT_PAYLOAD_TOKEN` (for payments)
- `TEST_DB_URL` (for tests)

### Database

PostgreSQL with SQLAlchemy ORM. Configure connection in `alembic.ini` (see `alembic.ini.example`).

## Testing

Tests use `unittest.TestCase` with custom `Common` base class that manages test database sessions. Requires `TEST_DB_URL` pointing to a test database.
