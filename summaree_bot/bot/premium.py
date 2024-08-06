import datetime as dt
import json
import os
from datetime import datetime, timedelta
from typing import Mapping, Optional, Sequence, Union, cast

from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ..models import (
    EmailToken,
    Invoice,
    InvoiceStatus,
    PremiumPeriod,
    Product,
    Subscription,
    SubscriptionStatus,
    SubscriptionType,
    TelegramChat,
    TelegramUser,
    User,
)
from ..models.session import DbSessionContext
from ..utils import url
from . import BotInvoice, BotMessage
from .db import ensure_chat, session_context
from .helpers import escape_markdown

__all__ = [
    "premium_handler",
    "precheckout_callback",
    "payment_callback",
    "referral_handler",
    "successful_payment_callback",
]


STRIPE_TOKEN = os.getenv("STRIPE_TOKEN", "Configure me in .env")
PAYMENT_PAYLOAD_TOKEN = os.getenv("PAYMENT_PAYLOAD_TOKEN", "Configure me in .env")


@session_context
@ensure_chat
def _referral_handler(update: Update, context: DbSessionContext) -> BotMessage:
    if update is None or update.message is None or update.effective_user is None:
        raise ValueError("update/message/user is None")
    # case 1: telegram_user has no user -> /register first
    session = context.db_session
    tg_user = session.get(TelegramUser, update.effective_user.id)
    chat_id = update.message.chat.id
    if tg_user is None or tg_user.user is None:
        msg = escape_markdown("✋💸 In order to use referrals, please `/register` your email first. 📧")
        return BotMessage(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN_V2)
    elif not tg_user.user.email_token.active:
        return BotMessage(
            chat_id=chat_id,
            text=escape_markdown(
                "✋ Your email is not verified. Please check your inbox and click the link in the email. "
                "Use `/register` to re-send email or change email address."
            ),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    elif not context.args:
        # case 2: no context.args -> list token and referred users
        n_referrals = len(tg_user.user.referrals)
        return BotMessage(
            chat_id=chat_id,
            text=escape_markdown(
                f"👥 Your referral token is `{tg_user.user.referral_token}`.\n\n"
                f"💫 You have referred {n_referrals} users. "
                f"In total, you have received {n_referrals*7} days of free premium! 💸"
            ),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    # case 3: context.args[0] is a token -> add referral
    else:
        stmt = select(User).where(User.referral_token == context.args[0])
        referrer = session.execute(stmt).scalar_one_or_none()
        if referrer is None:
            return BotMessage(
                chat_id=chat_id, text="🤷‍♀️🤷‍♂️ This referral token is not valid.", parse_mode=ParseMode.MARKDOWN_V2
            )
        else:
            tg_user.user.referrer = referrer
            return BotMessage(
                chat_id=chat_id,
                text=(
                    "👍 You have successfully used this referral token. "
                    "You and the referrer will both receive one week of premium for free! 💫💸"
                ),
                parse_mode=ParseMode.MARKDOWN_V2,
            )


async def referral_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bot_msg = _referral_handler(update, context)
    await bot_msg.send(context.bot)


def get_subscription_keyboard(
    context: DbSessionContext, subscription_id: Optional[int] = None, return_products: bool = False
) -> Union[InlineKeyboardMarkup, tuple[InlineKeyboardMarkup, Mapping[PremiumPeriod, Product]]]:
    callback_data: dict[str, Union[str, Sequence, Mapping]] = {"fnc": "buy_or_extend_subscription"}
    if subscription_id is not None:
        callback_data["args"] = [subscription_id]

    # fetch all ⭐ products
    stmt = select(Product).where(Product.premium_period.in_(list(PremiumPeriod))).where(Product.currency == "XTR")
    products = context.db_session.execute(stmt).scalars().all()
    if len(products) < len(PremiumPeriod):
        raise ValueError(
            f"Found less product than PremiumPeriods\nproducts: {products}\nPremiumPeriods: {PremiumPeriod}"
        )
    periods_to_products = {product.premium_period: product for product in products}

    # create keyboard
    period_to_keyboard_button_text = {
        PremiumPeriod.MONTH: f"🤖 1 month: ⭐{periods_to_products[PremiumPeriod.MONTH].discounted_price}",
        PremiumPeriod.QUARTER: f"💯 3 months: ⭐{periods_to_products[PremiumPeriod.QUARTER].discounted_price}",
        PremiumPeriod.YEAR: f"🔥 1 year: ⭐{periods_to_products[PremiumPeriod.YEAR].discounted_price}",
    }
    keyboard_buttons = [
        [
            InlineKeyboardButton(
                text, callback_data=dict(**callback_data, kwargs={"product_id": periods_to_products[period].id})
            )
        ]
        for period, text in period_to_keyboard_button_text.items()
    ]
    keyboard_buttons.append([InlineKeyboardButton("😌 No, thanks", callback_data={"fnc": "remove_inline_keyboard"})])
    if return_products:
        return InlineKeyboardMarkup(keyboard_buttons), periods_to_products
    else:
        return InlineKeyboardMarkup(keyboard_buttons)


@session_context
@ensure_chat
def _premium_handler(update: Update, context: DbSessionContext) -> BotMessage:
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
        reply_markup = get_subscription_keyboard(context, subscriptions[0].id)
        return BotMessage(chat_id=update.effective_chat.id, text=subscription_msg, reply_markup=reply_markup)
    # case 3: chat has no active subscription
    #  -> check if user is in database, if not -> create
    #  -> ask user if subscription should be bought
    else:
        tg_user = session.get(TelegramUser, update.effective_user.id)
        if not tg_user.user:
            user = User(telegram_user=tg_user, email_token=EmailToken())
            session.add(user)
        reply_markup = get_subscription_keyboard(context)
        return BotMessage(
            chat_id=update.effective_chat.id,
            text=escape_markdown("⌛ You have no active subscription. Would you like to buy one?"),
            reply_markup=reply_markup,
        )


async def premium_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bot_msg = _premium_handler(update, context)
    await context.bot.send_message(**bot_msg)


@session_context
@ensure_chat
def _payment_callback(update: Update, context: DbSessionContext, product_id: int) -> BotInvoice:
    """Sends an invoice without shipping-payment."""
    if update.effective_chat is None or update.effective_user is None:
        raise ValueError("chat/user is None")
    session = context.db_session
    product = session.get(Product, product_id)
    if product is None:
        raise ValueError(f"product {product_id} not found in database")

    title = "summar.ee premium subscription"
    description = "\n".join(["Premium features:", "- Unlimited summaries", "- unlimited translations"])
    # In order to get a provider_token see https://core.telegram.org/bots/payments#getting-a-token
    currency = "XTR"
    # price in dollars
    price = product.discounted_price
    days = product.premium_period.value
    prices = [LabeledPrice(f"Premium Subscription {days} days", price)]

    chat_id = update.effective_chat.id
    tg_user = session.get(TelegramUser, update.effective_user.id)

    invoice = Invoice(tg_user_id=tg_user.id, chat_id=chat_id, product_id=product.id)
    session.add(invoice)

    payload = url.encode([PAYMENT_PAYLOAD_TOKEN, invoice.id])

    # optionally pass need_name=True, need_phone_number=True,
    # need_email=True, need_shipping_address=True, is_flexible=True
    return BotInvoice(
        chat_id=chat_id,
        title=title,
        description=description,
        payload=str(payload, "ascii"),
        provider_token=STRIPE_TOKEN,
        currency=currency,
        prices=prices,
        need_email=False,
        protect_content=True,
    )


async def payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, product_id: int) -> None:
    bot_invoice = _payment_callback(update, context, product_id)
    await bot_invoice.send(context.bot)


def check_payment_payload(context: DbSessionContext, invoice_payload: str) -> int:
    payload = cast(Sequence, url.decode(invoice_payload))
    payload_token, invoice_id = payload
    if payload_token != PAYMENT_PAYLOAD_TOKEN:
        session = context.db_session
        invoice = session.get(Invoice, invoice_id)
        if invoice is not None:
            invoice.status = InvoiceStatus.canceled
        raise ValueError(f"payload_token ({payload_token}) != PAYMENT_PAYLOAD_TOKEN ({PAYMENT_PAYLOAD_TOKEN})")
    return invoice_id


@session_context
def _precheckout_callback(update: Update, context: DbSessionContext) -> bool:
    """Answers the PrecheckoutQuery"""
    query = update.pre_checkout_query
    if query is None:
        raise ValueError("update.pre_checkout_query is None")
    # check the payload, is this from your bot?
    check_payment_payload(context, query.invoice_payload)
    return True


async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Answers the PrecheckoutQuery"""
    query = update.pre_checkout_query
    if query is None:
        raise ValueError("update.pre_checkout_query is None")

    try:
        is_ok = _precheckout_callback(update, context)
    except (json.JSONDecodeError, ValueError):
        await query.answer(
            ok=False, error_message="😕 Something went wrong... Invoice has been cancelled. Support has been contacted."
        )
        # TODO: write a message into admin channel
        raise

    await query.answer(ok=is_ok)


# finally, after contacting the payment provider...
@session_context
@ensure_chat
def _successful_payment_callback(update: Update, context: DbSessionContext) -> BotMessage:
    """Confirms the successful payment."""
    context = cast(DbSessionContext, context)
    if (
        update.message is None
        or (payment := update.message.successful_payment) is None
        or (_tg_user := update.effective_user) is None
    ):
        raise ValueError("update.message.successful_payment is None")

    session = context.db_session
    invoice_id = check_payment_payload(context, payment.invoice_payload)
    # create subscription
    invoice = session.get(Invoice, invoice_id)
    if not invoice:
        raise ValueError("Invoice not found")

    if (_tg_user := update.effective_user) is None:
        raise ValueError("telegram user is None")
    # check if invoice user has changed (e.g. forwarded invoice)
    tg_user = session.get(TelegramUser, _tg_user.id)
    if tg_user is None or tg_user.user is None:
        raise ValueError("(telegram) user not found in database")

    if invoice.user != tg_user.user:
        invoice.user = tg_user.user
    invoice.status = InvoiceStatus.paid

    # create subscription
    start_date = datetime.now(dt.UTC)
    end_date = start_date + timedelta(days=invoice.product.premium_period.value)
    subscription = Subscription(
        user=invoice.user,
        chat=invoice.chat,
        start_date=start_date,
        end_date=end_date,
        status=SubscriptionStatus.active,
        type=SubscriptionType.paid,
    )
    session.add(subscription)

    return BotMessage(
        chat_id=update.message.chat_id, text=f"Thank you for your payment! Subscription is active until {end_date}"
    )


async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bot_msg = _successful_payment_callback(update, context)
    await bot_msg.send(context.bot)
