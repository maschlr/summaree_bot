import asyncio
import bz2
import datetime as dt
import io
import json
import os
from datetime import datetime, timedelta

import pandas as pd
import prettytable as pt
from sqlalchemy import func, select
from telegram import InputMediaPhoto, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ..integrations.openai import summary_prompt_file_path
from ..models import Summary, TelegramUser
from ..models.session import DbSessionContext, session_context
from . import AdminChannelMessage, BotDocument
from .helpers import escape_markdown


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
    msg, media = _stats(update, context)
    async with asyncio.TaskGroup() as tg:
        tg.create_task(msg.send(context.bot))
        tg.create_task(context.bot.send_media_group(msg.chat_id, media))


@session_context
def _stats(update: Update, context: DbSessionContext) -> AdminChannelMessage:
    if update.message is None:
        raise ValueError("Update needs a message")

    session = context.db_session
    summaries = session.scalars(select(Summary)).all()

    users = set(filter(lambda user: user is not None, (s.tg_user_id for s in summaries)))
    users = {s.tg_user_id for s in summaries if s.tg_user_id is not None}
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
        row_summaries = [s for s in summaries if s.created_at.replace(tzinfo=dt.UTC) >= row_timespan]
        row_users = {s.tg_user_id for s in row_summaries if s.tg_user_id is not None}
        table.add_row([row_label, len(row_users), len(row_summaries)])

    table.add_row(total_row_data)

    usage_stats = Summary.get_usage_stats(session)
    # create two messages with plots
    # 1. summaries per day/month
    # 2. active users per day/month
    date_index, summary_count, user_count = zip(*usage_stats, strict=True)
    dt_index = pd.to_datetime(date_index, utc=True)
    usage_df = pd.DataFrame(index=dt_index, data={"Summaries": summary_count, "Users": user_count})
    from_date = dt.datetime.now(dt.UTC) - dt.timedelta(days=30)
    daily_data = usage_df[from_date:].sort_index(ascending=False)
    ax_daily = daily_data.plot(title="Summaries and active users per day", kind="barh")
    ax_daily.set_yticklabels([d.strftime("%Y-%m-%d") for d in daily_data.index])
    ax_daily.set_xlabel("Count")
    ax_daily.set_ylabel("Date")

    daily_buffer = io.BytesIO()
    ax_daily.get_figure().savefig(daily_buffer)

    monthly_data = usage_df.resample("ME").sum().sort_index(ascending=False)
    ax_monthly = monthly_data.plot(title="Summaries and active users per month", kind="barh")
    ax_monthly.set_yticklabels([d.strftime("%Y-%m") for d in monthly_data.index])
    ax_monthly.set_xlabel("Count")
    ax_monthly.set_ylabel("Date")

    monthly_buffer = io.BytesIO()
    ax_monthly.get_figure().savefig(monthly_buffer)

    media = [InputMediaPhoto(b.getvalue()) for b in [daily_buffer, monthly_buffer]]

    msg = AdminChannelMessage(
        text=f"```{table}```",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return msg, media


async def top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = _top(update, context)
    await msg.send(context.bot)


@session_context
def _top(update: Update, context: DbSessionContext):
    session = context.db_session

    result = session.query(Summary.tg_user_id, func.count(Summary.id).label("count")).group_by(Summary.tg_user_id).all()

    result.sort(key=lambda x: x[1], reverse=True)

    table = pt.PrettyTable(["User", "Summaries"])
    table.align["User"] = "l"
    table.align["Summaries"] = "r"

    for user_id, count in result:
        user = session.get(TelegramUser, user_id)
        if not user:
            continue
        table.add_row([f"{user.username or user.first_name} ({user.id})", count])

    msg = AdminChannelMessage(
        text=f"```{table}```",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return msg


async def activate_referral_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Asynchronous handler for the /activate command."""
    msg = _activate_referral_code(update, context)
    await msg.send(context.bot)


@session_context
def _activate_referral_code(update: Update, context: DbSessionContext) -> AdminChannelMessage:
    """Synchronous part of the /activate command."""
    session = context.db_session
    stmt = (
        select(TelegramUser)
        .where(TelegramUser.username == context.args[0])
        .where(TelegramUser.referral_token_active.is_(False))
    )
    user = session.execute(stmt).scalar_one_or_none()
    if user is None:
        return AdminChannelMessage(
            text=(
                f"User {context.args[0]} not found or referral code already activated. "
                "Use /list to see all active referral codes."
            )
        )
    user.referral_token_active = True

    return AdminChannelMessage(text=f"Referral code activated for {user.md_link}", parse_mode=ParseMode.MARKDOWN_V2)


async def list_referral_codes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler for the /list command to list all active referral codes."""
    msg = _list_referral_codes(update, context)
    await msg.send(context.bot)


@session_context
def _list_referral_codes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> AdminChannelMessage:
    """Synchronous part of the /list command to list all active referral codes."""
    session = context.db_session

    stmt = select(TelegramUser).where(TelegramUser.referral_token_active)
    users = session.execute(stmt).scalars().all()

    if users:
        prefix = "Active referral codes:\n"
        body = "\n".join([f"\- {user.md_link}: {escape_markdown(user.referral_url)}" for user in users])

        msg = AdminChannelMessage(
            text=prefix + body,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    else:
        msg = AdminChannelMessage(text="No active referral codes found.")
    return msg


async def deactivate_referral_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Asynchronous handler for the /deactivate command."""
    msg = _deactivate_referral_code(update, context)
    await msg.send(context.bot)


@session_context
def _deactivate_referral_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> AdminChannelMessage:
    """Syncronous part of the /deactivate command"""
    session = context.db_session
    stmt = (
        select(TelegramUser).where(TelegramUser.username == context.args[0]).where(TelegramUser.referral_token_active)
    )
    user = session.execute(stmt).scalar_one_or_none()
    if user is None:
        return AdminChannelMessage(
            text=(
                f"User {context.args[0]} not found or referral code not active. "
                "Use /list to see all active referral codes."
            )
        )
    user.referral_token_active = False

    return AdminChannelMessage(text=f"Referral code deactivated for {user.username}")
