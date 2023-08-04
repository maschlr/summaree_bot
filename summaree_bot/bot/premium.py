import json
import os
import secrets
from datetime import datetime, timedelta
from typing import Mapping, Optional, Sequence, Union, cast

from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Update
from telegram.ext import ContextTypes

from ..logging import getLogger
from ..models import (
    Subscription,
    SubscriptionStatus,
    SubscriptionType,
    TelegramChat,
    TelegramUser,
    User,
)
from ..models.session import DbSessionContext
from ..utils import url
from .db import add_session, ensure_chat

_logger = getLogger(__name__)

__all__ = [
    "premium_handler",
    "precheckout_callback",
    "payment_callback",
    "referral_handler",
    "successful_payment_callback",
]


STRIPE_TOKEN = os.getenv("STRIPE_TOKEN", "")
PAYMENT_PAYLOAD_TOKEN = secrets.token_urlsafe(8)


@add_session
@ensure_chat
async def referral_handler(update: Update, context: DbSessionContext) -> None:
    if update is None or update.message is None or update.effective_user is None:
        raise ValueError("update/message/user is None")
    # case 1: telegram_user has no user -> /register first
    session = context.db_session
    tg_user = session.get(TelegramUser, update.effective_user.id)
    if tg_user is None or tg_user.user is None:
        msg = "✋💸 In order to use referrals, please `/register` your email first\. 📧"
        await update.message.reply_markdown_v2(msg)
        return
    elif not tg_user.user.email_token.active:
        await update.message.reply_markdown_v2(
            "✋💸 Your email is not verified\. Please check your inbox and click the link in the email\. "
            "Use `/register` to re-send email or change email address\."
        )
    elif not context.args:
        # case 2: no context.args -> list token and referred users
        n_referrals = len(tg_user.user.referrals)
        await update.message.reply_markdown_v2(
            f"👥 Your referral token is `{tg_user.user.referral_token}`\.\n\n"
            f"💫 You have referred {n_referrals} users\. "
            f"In total, you have received {n_referrals*7} days of free premium\! 💸"
        )
    # case 3: context.args[0] is a token -> add referral
    else:
        stmt = select(User).where(User.referral_token == context.args[0])
        referrer = session.execute(stmt).scalar_one_or_none()
        if referrer is None:
            await update.message.reply_markdown_v2("🤷‍♀️🤷‍♂️ This referral token is not valid\.")
        else:
            tg_user.user.referrer = referrer
            await update.message.reply_markdown_v2(
                "👍 You have successfully used this referral token\. "
                "You and the referrer will both receive one week of premium for free! 💫💸"
            )


def generate_subscription_keyboard(subscription_id: Optional[int] = None) -> InlineKeyboardMarkup:
    callback_data: dict[str, Union[str, Sequence, Mapping]] = {"fnc": "buy_or_extend_subscription"}
    if subscription_id is not None:
        callback_data["args"] = [subscription_id]
    keyboard = [
        [
            InlineKeyboardButton("👶 1 month: 1,99€", callback_data=dict(**callback_data, kwargs={"days": 31})),
        ],
        [
            InlineKeyboardButton("🆙 3 months: 4,20€", callback_data=dict(**callback_data, kwargs={"days": 92})),
        ],
        [
            InlineKeyboardButton("💯 1 year: 9,99€", callback_data=dict(**callback_data, kwargs={"days": 366})),
        ],
        [InlineKeyboardButton("🙅‍♀️🙅‍♂️ No, thanks", callback_data={"fnc": "remove_inline_keyboard"})],
    ]
    return InlineKeyboardMarkup(keyboard)


@add_session
@ensure_chat
async def premium_handler(update: Update, context: DbSessionContext) -> None:
    if update.effective_chat is None or update.effective_message is None:
        raise ValueError("update/chat is None")

    session = context.db_session

    chat = session.get(TelegramChat, update.effective_chat.id)
    if chat is None:
        raise ValueError("chat is None")

    stmt = (
        select(Subscription)
        .where(Subscription.chat_id == chat.id)
        .where(Subscription.status.in_([SubscriptionStatus.active, SubscriptionStatus.extended]))
        .order_by(Subscription.end_date.asc())
    )
    # case 1: chat has active subscription
    #   -> show subscription info
    #   -> ask user if subscription should be extended
    if subscriptions := session.execute(stmt).scalars().all():
        subscription_msg = "🌟 You have active subscription(s): \n"
        for subscription in subscriptions:
            subscription_msg += (
                f"- 📅 {subscription.start_date} - {subscription.end_date}: "
                f"({subscription.status} @ chat {subscription.chat.title or subscription.chat.username})\n"
            )

        subscription_msg += "\nWould you like to extend your subscription?"
        reply_markup = generate_subscription_keyboard(subscriptions[0].id)
        await update.effective_message.reply_markdown_v2(subscription_msg, reply_markup=reply_markup)
    # case 3: chat has no active subscription
    #  -> ask user if subscription should be bought
    else:
        reply_markup = generate_subscription_keyboard()
        await update.effective_message.reply_markdown_v2(
            "⌛ You have no active subscription\. Would you like to buy one?", reply_markup=reply_markup
        )


@add_session
async def payment_callback(update: Update, context: DbSessionContext, days: int) -> None:
    if update.message is None:
        raise ValueError("update/message is None")
    """Sends an invoice without shipping-payment."""
    days_to_price_eur = {31: 199, 92: 420, 366: 999}
    if days not in days_to_price_eur:
        raise ValueError(f"days ({days}) not in days_to_price_eur")

    chat_id = update.message.chat_id
    title = "summar.ee bot Subscription"
    description = "Premium Features: Unlimited summaries, unlimited translations"
    # In order to get a provider_token see https://core.telegram.org/bots/payments#getting-a-token
    currency = "EUR"
    # price in dollars
    price = days_to_price_eur[days]
    # price * 100 so as to include 2 decimal points
    prices = [LabeledPrice(f"Premium Subscription {days} days", price * 100)]

    # optionally pass need_name=True, need_phone_number=True,
    # need_email=True, need_shipping_address=True, is_flexible=True
    payload = url.encode([PAYMENT_PAYLOAD_TOKEN, days])
    await context.bot.send_invoice(chat_id, title, description, str(payload, "ascii"), STRIPE_TOKEN, currency, prices)


def check_payment_payload(query) -> int:
    payload = cast(Sequence, url.decode(query.invoice_payload))
    payload_token, days = payload
    if payload_token != PAYMENT_PAYLOAD_TOKEN:
        raise ValueError(f"payload_token ({payload_token}) != PAYMENT_PAYLOAD_TOKEN ({PAYMENT_PAYLOAD_TOKEN})")
    return days


async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Answers the PreQecheckoutQuery"""
    query = update.pre_checkout_query
    if query is None:
        raise ValueError("update.pre_checkout_query is None")
    # check the payload, is this from your bot?
    try:
        check_payment_payload(query)
    except (json.JSONDecodeError, ValueError):
        await query.answer(ok=False, error_message="😕 Something went wrong...Support has been contacted.")
        raise

    await query.answer(ok=True)


# finally, after contacting the payment provider...
async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context = cast(DbSessionContext, context)
    """Confirms the successful payment."""
    query = update.callback_query
    if (
        query is None
        or update.effective_user is None
        or update.effective_chat is None
        or update.effective_message is None
    ):
        raise ValueError("update doesn't contain necessary information. See traceback for more details")
    session = context.db_session
    days = check_payment_payload(query)
    # create subscription
    tg_user = session.get(TelegramUser, update.effective_user.id)
    if not tg_user:
        raise ValueError("tg_user is None")
    chat = session.get(TelegramChat, update.effective_chat.id)
    user = tg_user.user
    start_date = datetime.utcnow()
    end_date = start_date + timedelta(days=days)
    subscription = Subscription(
        user=user,
        chat=chat,
        start_date=start_date,
        end_date=end_date,
        status=SubscriptionStatus.active,
        type=SubscriptionType.paid,
    )
    session.add(subscription)

    await update.effective_message.reply_text(f"Thank you for your payment! Subscription is active until {end_date}")
