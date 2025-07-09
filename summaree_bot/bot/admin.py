import asyncio
import bz2
import datetime as dt
import io
import json
import os
from datetime import datetime, timedelta
from typing import Generator, Union

import pandas as pd
import prettytable as pt
from sqlalchemy import extract, func, select
from telegram import InputMediaPhoto, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown, mention_html

from ..models import (
    Invoice,
    InvoiceStatus,
    Subscription,
    Summary,
    TelegramChat,
    TelegramUser,
)
from ..models.session import DbSessionContext, session_context
from . import AdminChannelMessage, BotDocument, BotMessage
from .premium import create_subscription


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
        chat_id=update.effective_message.chat_id,
        reply_to_message_id=update.effective_message.message_id,
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
    monthly_data = usage_df.resample("ME").sum().sort_index(ascending=False)

    # dataset, title, strftime
    plot_data = (
        [daily_data, "Summaries and active users per day", "%Y-%m-%d"],
        [monthly_data, "Summaries and active users per month", "%Y-%m"],
    )

    media = []
    for data, title, strftime in plot_data:
        ax = data.plot(title=title, kind="barh")
        ax.set_yticklabels([d.strftime(strftime) for d in data.index])
        ax.set_xlabel("Count")
        ax.set_ylabel("Date")
        buffer = io.BytesIO()
        ax.get_figure().savefig(buffer, bbox_inches="tight")
        media.append(InputMediaPhoto(buffer.getvalue()))
        buffer.close()

    msg = AdminChannelMessage(
        text=f"```{table}```",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return msg, media


async def top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Async handler for getting the /top users"""
    for msg in _top(update, context):
        await msg.send(context.bot)


@session_context
def _top(_update: Update, context: DbSessionContext) -> Generator[AdminChannelMessage, None, None]:
    session = context.db_session
    now = dt.datetime.now(dt.UTC)
    month = now.month
    month_name = now.strftime("%B")
    result = (
        session.query(Summary.tg_user_id, func.count(Summary.id).label("count"))
        .filter(extract("month", Summary.created_at) == month)
        .group_by(Summary.tg_user_id)
        .all()
    )
    result.sort(key=lambda x: x[1], reverse=True)

    table = pt.PrettyTable(["Rank", "User", f"Summaries ({month_name})"])
    table.align["Rank"] = "r"
    table.align["User"] = "l"
    table.align["Summaries"] = "r"

    rank = 1
    step = 42
    for i in range(0, len(result), step):
        for user_id, count in result[i : i + step]:
            user = session.get(TelegramUser, user_id)
            if not user:
                continue
            table.add_row(
                [rank, f"{user.username or user.first_name}" + (" ⭐" if user.is_premium_active else ""), count]
            )
            rank += 1

        yield AdminChannelMessage(
            text=f"```{table}```",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        table.clear_rows()


async def activate_referral_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Asynchronous handler for the /activate command."""
    msg = _activate_referral_code(update, context)
    await msg.send(context.bot)


@session_context
def _activate_referral_code(update: Update, context: DbSessionContext) -> AdminChannelMessage:
    """Synchronous part of the /activate command."""
    session = context.db_session

    try:
        user = TelegramUser.get_by_id_or_username(session, context.args[0])
    except IndexError:
        return AdminChannelMessage(text="Usage: /activate <user_id_or_username>")

    if user is None or user.referral_token_active:
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
        body = "\n".join([f"\- {user.md_link}: {escape_markdown(user.referral_url, version=2)}" for user in users])

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
    try:
        user = TelegramUser.get_by_id_or_username(session, context.args[0])
    except IndexError:
        return AdminChannelMessage(text="Usage: /deactivate <user_id_or_username>")

    if user is None or not user.referral_token_active:
        return AdminChannelMessage(
            text=(
                f"User {context.args[0]} not found or referral code not active. "
                "Use /list to see all active referral codes."
            )
        )
    user.referral_token_active = False

    return AdminChannelMessage(text=f"Referral code deactivated for {user.md_link}", parse_mode=ParseMode.MARKDOWN_V2)


async def get_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler for the /get_file command to get a file from the bot."""
    file_id = context.args[0]
    msg = BotDocument(chat_id=update.effective_chat.id, document=file_id)
    await msg.send(context.bot)


async def forward_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler for the /forward command to forward a message to the admin chat."""
    chat_id, msg_id = context.args[0]
    await context.application.bot.forward_message(
        chat_id=update.effective_chat.id, from_chat_id=chat_id, message_id=msg_id
    )


async def gift_premium(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler for the /gift_premium command to gift premium to a user."""
    for msg in _gift_premium(update, context):
        await msg.send(context.bot)


@session_context
def _gift_premium(
    update: Update, context: DbSessionContext
) -> Generator[Union[AdminChannelMessage, BotMessage], None, None]:
    """Synchronous part of the /gift_premium command to gift premium to a user."""
    session = context.db_session
    try:
        chat_id_or_username, days = context.args
    except ValueError:
        yield AdminChannelMessage(text="Usage: /gift <chat_id_or_username> <days>")
        return

    try:
        chat_id = int(chat_id_or_username)
        chat = session.get(TelegramChat, chat_id)
    except ValueError:
        chat = session.scalar(select(TelegramChat).where(TelegramChat.username == chat_id_or_username))

    if chat is None:
        yield AdminChannelMessage(text=f"Chat {chat_id_or_username} not found.")
        return

    subscription: Subscription = create_subscription(update, session, chat.id, int(days), to_be_paid=False)
    sub_end_date_str = subscription.end_date.strftime("%x")

    if subscription.chat_id > 0:
        # private chat
        lang_to_text = {
            "en": f"🎁 A gift for you: summar.ee premium until {sub_end_date_str})",
            "de": f"🎁 Ein Geschenk für dich: summar.ee Premium features bis zum {sub_end_date_str}",
            "es": f"🎁 Un regalo para ti: summar.ee Premium features hasta el {sub_end_date_str}",
            "ru": f"🎁 Подарок для вас: summar.ee Premium до {sub_end_date_str}",
        }
    else:
        # group chat
        lang_to_text = {
            "en": f"🎁 Premium gifted to {mention_html(chat.id, chat.title or chat.username)} until {sub_end_date_str}",
            "de": (
                f"🎁 Premium für {mention_html(chat.id, chat.title or chat.username)}"
                f" bis zum {sub_end_date_str} geschenkt"
            ),
            "es": (
                f"🎁 Premium regalado a {mention_html(chat.id, chat.title or chat.username)}"
                f" hasta el {sub_end_date_str}"
            ),
            "ru": f"🎁 Премиум подарен {mention_html(chat.id, chat.title or chat.username)} до {sub_end_date_str}",
        }
    text = lang_to_text.get(chat.language.code, lang_to_text["en"])

    yield BotMessage(chat_id=chat.id, text=text)
    yield AdminChannelMessage(
        text=f"Premium gifted to {mention_html(chat.id, chat.title or chat.username)} until {sub_end_date_str}",
        parse_mode=ParseMode.HTML,
    )


async def list_invoices(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler for the /list_invoices command to list all invoices."""
    msg = _list_invoices(update, context)
    await msg.send(context.bot)


@session_context
def _list_invoices(update: Update, context: DbSessionContext) -> AdminChannelMessage:
    """Synchronous part of the /list_invoices command to list all invoices."""
    invoice_stmt = (
        select(Invoice).where(Invoice.status == InvoiceStatus.paid).order_by(Invoice.paid_at.desc()).limit(50)
    )
    invoices = context.db_session.execute(invoice_stmt).scalars().all()
    table = pt.PrettyTable(["ID", "User", "UserID", "ChannelID", "Amount", "Paid At"])
    table.align["ID"] = "r"
    table.align["User"] = "l"
    table.align["UserID"] = "l"
    table.align["ChannelID"] = "l"
    table.align["Amount"] = "r"
    table.align["Paid At"] = "l"

    for invoice in invoices:
        table.add_row(
            [
                invoice.id,
                invoice.tg_user.username or invoice.tg_user.first_name,
                invoice.tg_user.id,
                invoice.subscription.chat.id,
                invoice.total_amount,
                invoice.paid_at,
            ]
        )

    return AdminChannelMessage(text=f"```{table}```", parse_mode=ParseMode.MARKDOWN_V2)


command_to_handler = {
    "dataset": (dataset, r"Download the full dataset"),
    "stats": (stats, r"Show usage stats"),
    "top": (top, r"Show top users"),
    "list_referrals": (list_referral_codes, r"List all active referral codes"),
    "activate": (activate_referral_code, r"Activate a referral code\. Usage: `/activate <user_id_or_username>`"),
    "deactivate": (
        deactivate_referral_code,
        r"Deactivate a referral code\. Usage: `/deactivate <user_id_or_username>`",
    ),
    "get_file": (get_file, r"Get a file from the bot\. Usage: `/get_file <file_id>`"),
    "forward_msg": (forward_msg, r"Forward a message to the admin chat\. Usage: `/forward <chat_id> <message_id>`"),
    "gift": (gift_premium, r"Gift premium to a user\. Usage: `/gift <user_id_or_username> <days>`"),
    "list_invoices": (list_invoices, r"List all paid invoices"),
}


async def help_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler for the /help_admin command to list all admin commands."""
    md_text = "\n".join(
        f"/{escape_markdown(command, version=2)}: {description}"
        for command, (_handler, description) in command_to_handler.items()
    )
    msg = AdminChannelMessage(text=md_text, parse_mode=ParseMode.MARKDOWN_V2)
    await msg.send(context.bot)


command_to_handler["helpadmin"] = (help_admin, "List all admin commands")
