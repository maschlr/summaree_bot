import bz2
import datetime as dt
import io
import json
import os
from datetime import datetime, timedelta

import prettytable as pt
from sqlalchemy import select
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ..integrations.openai import summary_prompt_file_path
from ..models import Summary
from ..models.session import DbSessionContext, session_context
from . import BotDocument, BotMessage


async def dataset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bot_msg = _dataset(update, context)
    await bot_msg.send(context.bot)


@session_context
def _dataset(update: Update, context: DbSessionContext) -> BotDocument:
    admin_chat_id = os.getenv("ADMIN_CHAT_ID")
    if update.message is None:
        raise ValueError("Update needs a message")
    elif admin_chat_id is None:
        raise ValueError("ADMIN_CHAT_ID environment variable not set")

    with open(summary_prompt_file_path) as fp:
        system_msg = fp.read()

    session = context.db_session

    summaries = session.execute(select(Summary)).scalars().all()
    data_buffer = io.BytesIO()
    for summary in summaries:
        if summary.transcript.input_language is None:
            continue
        assistant_msg_data = {
            "language": summary.transcript.input_language.ietf_tag,
            "topics": [topic.text for topic in summary.topics],
        }
        assistant_msg = json.dumps(assistant_msg_data)
        data = {
            "messages": [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": summary.transcript.result},
                {"role": "agent", "content": assistant_msg},
            ]
        }
        data_buffer.write(json.dumps(data).encode())
        data_buffer.write("\n".encode())

    compressed_buffer = io.BytesIO()
    compressed_buffer.write(bz2.compress(data_buffer.getvalue()))

    now = datetime.now(dt.UTC)
    filename = f"dataset-{now.isoformat()[:19]}.jsonl.bz2"
    bot_msg = BotDocument(
        chat_id=update.message.chat_id,
        reply_to_message_id=update.message.message_id,
        filename=filename,
        document=compressed_buffer.getvalue(),
    )
    return bot_msg


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = _stats(update, context)
    await msg.send(context.bot)


@session_context
def _stats(update: Update, context: DbSessionContext) -> BotMessage:
    if update.message is None:
        raise ValueError("Update needs a message")

    session = context.db_session
    summaries = session.scalars(select(Summary)).all()

    users = set(filter(lambda user: user is not None, (s.tg_user_id for s in summaries)))
    total_row_data = ["Total", len(users), len(summaries)]

    table = pt.PrettyTable(["Time", "Users", "Summaries"])
    table.align["Time"] = "l"
    table.align["Users"] = "r"
    table.align["Summaries"] = "r"

    now = datetime.now(dt.UTC)

    label_to_datetime = {
        "24h": now - timedelta(hours=24),
        "7 days": now - timedelta(days=7),
        "30 days": now - timedelta(days=30),
    }

    for row_label, row_timespan in label_to_datetime.items():
        row_summaries = list(filter(lambda s: s.created_at > row_timespan, summaries))
        row_users = set(filter(lambda user: user is not None, (s.tg_user_id for s in row_summaries)))
        table.add_row([row_label, len(row_users), len(row_summaries)])

    table.add_row(total_row_data)

    msg = BotMessage(
        chat_id=update.message.chat_id,
        text=f"```{table}```",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return msg
