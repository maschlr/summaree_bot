# Telegram Bot summar.ee

## Description

Telegram for transcribing, summarizing and translating audio messages.

## Setup

### Create virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
```

### Install dependencies

```bash
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

### Database initialization (optional)

```bash
python -m scripts.create_database
```

## Configuration

### Environment variabels

See `summaree_bot/env.ini.example` for environment variables that need to be set.

### Database

1. Copy `alembic.ini.example`
2. Configure [postgresql database url](https://docs.sqlalchemy.org/en/20/core/engines.html#database-urls) in `alembic.ini`

## Database creation

1. `alembic revision -m "\<message\>" --autogenerate``
2. adapt migration script created in `alembic/versions/`
3. Run migration: `alembic upgrade head`

See [alembic documentation](https://alembic.sqlalchemy.org/en/latest/tutorial.html#running-our-first-migration) for more options

## Database export/import
