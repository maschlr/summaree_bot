<a href="https://summar.ee">
  <img src="https://raw.githubusercontent.com/maschlr/summaree_bot/master/logo.svg" alt="drawing" width="128"/>
</a>

# AI Chatbot [summar.ee](https://summar.ee)

## Description

[summar.ee](https://summar.ee) is an AI chatbot for transcribing, summarizing and translating voice messages and audio files.
The bot automatically processes voice messages and audio files when it's added to a chat.
You can talk to it in a private chat or add it to a group chat.

Currently, the bot is running on [Telegram](https://telegram.org/): **[@summaree_bot](https://t.me/summaree_bot)**

### Features

- [x] User interface in four languages: 🇺🇸 English, 🇩🇪 German, 🇷🇺 Russian and 🇪🇸 Spanish
- [x] Transcribe & translate voice messages & audio files
- [x] Create & translate summary of transcripts
- [x] Add hashtags to summaries for easy search & categorization

## Development

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
