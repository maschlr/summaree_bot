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
    Invoice,
    InvoiceStatus,
    PremiumPeriod,
    Product,
    Subscription,
    SubscriptionStatus,
    SubscriptionType,
    TelegramChat,
    TelegramUser,
)
from ..models.session import DbSessionContext
from ..utils import url
from . import AdminChannelMessage, BotInvoice, BotMessage
from .db import ensure_chat, session_context

__all__ = [
    "premium_handler",
    "precheckout_callback",
    "payment_callback",
    "referral_handler",
    "successful_payment_callback",
    "get_sale_text",
]


STRIPE_TOKEN = os.getenv("STRIPE_TOKEN", "Configure me in .env")
PAYMENT_PAYLOAD_TOKEN = os.getenv("PAYMENT_PAYLOAD_TOKEN", "Configure me in .env")


@session_context
@ensure_chat
def _referral_handler(update: Update, context: DbSessionContext) -> BotMessage:
    if update is None or update.message is None or update.effective_user is None:
        raise ValueError("update/message/user is None")
    session = context.db_session
    tg_user = session.get(TelegramUser, update.effective_user.id)
    chat_id = update.message.chat.id
    # case 1: referral token is not active
    if not tg_user.referral_token_active:
        text = "✋💸 In order to use referrals, your token needs to be activated\.\n Please contact `/support`\."
        return BotMessage(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN_V2)
    # case 2: list referrals and the total amount of stars
    else:
        n_referrals = len(tg_user.referrals)
        n_stars = 0
        for referred_user in tg_user.referrals:
            star_invoices = (invoice for invoice in referred_user.invoices if invoice.product.currency == "XTR")
            for invoice in star_invoices:
                n_stars += invoice.total_amount

        return BotMessage(
            chat_id=chat_id,
            text=(
                f"👥 Your referral token url is {tg_user.referral_url}\n\n"
                f"💫 You have referred {n_referrals} users. They have paid a total of {n_stars} ⭐"
            ),
        )


async def referral_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Async handler for listing the referrals"""
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
        .where(Subscription.tg_user_id == update.effective_user.id)
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
                f"- 📅 {subscription.start_date.strftime('%x')} - {subscription.end_date.strftime('%x')} "
                f"@ chat {subscription.chat.title or subscription.chat.username}\n"
            )

        subscription_msg += "\nWould you like to extend your subscription?"
        reply_markup = get_subscription_keyboard(context, subscriptions[0].id)
        return BotMessage(chat_id=update.effective_chat.id, text=subscription_msg, reply_markup=reply_markup)
    # case 2: chat has no active subscription
    #  -> check if user is in database, if not -> create
    #  -> ask user if subscription should be bought
    else:
        reply_markup, periods_to_products = get_subscription_keyboard(context, return_products=True)

        text = r"You currently have no active subscription\. " + get_sale_text(periods_to_products)
        return BotMessage(
            chat_id=update.effective_chat.id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN_V2,
        )


def get_sale_text(periods_to_products: Mapping[PremiumPeriod, Product]) -> str:
    return "".join(
        [
            "Premium is on SALE right NOW:\n",
            "\n".join(
                (
                    rf"\- {premium_period.value} days for ⭐{product.discounted_price} \(~{product.price}~ "
                    rf"➡️ {(1-product.discounted_price/product.price)*100:.0f}% OFF\!\)"
                )
                for premium_period, product in periods_to_products.items()
            ),
            "\n\nWould you like to buy premium?",
        ]
    )


async def premium_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bot_msg = _premium_handler(update, context)
    await bot_msg.send(context.bot)


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

    chat_id = update.effective_chat.id
    tg_user = session.get(TelegramUser, update.effective_user.id)

    # create subscription
    start_date = datetime.now(dt.UTC)
    end_date = start_date + timedelta(days=product.premium_period.value)
    subscription = Subscription(
        tg_user=tg_user,
        chat_id=chat_id,
        start_date=start_date,
        end_date=end_date,
        status=SubscriptionStatus.pending,
        type=SubscriptionType.paid,
        active=False,
    )
    session.add(subscription)

    title = "summar.ee premium subscription"
    # In order to get a provider_token see https://core.telegram.org/bots/payments#getting-a-token
    currency = "XTR"
    price = product.discounted_price
    days = product.premium_period.value
    description = (
        f"Premium features for {days} days (from {start_date.strftime('%x')} to {end_date.strftime('%x')}; "
        "ends automatically)"
    )
    prices = [LabeledPrice(description, price)]

    invoice = Invoice(tg_user_id=tg_user.id, chat_id=chat_id, product_id=product.id, subscription=subscription)
    session.add(invoice)

    # w/o flush, invoice has no id
    session.flush()

    payload = url.encode([PAYMENT_PAYLOAD_TOKEN, invoice.id])

    # optionally pass need_name=True, need_phone_number=True,
    # need_email=True, need_shipping_address=True, is_flexible=True
    return BotInvoice(
        chat_id=chat_id,
        title=title,
        description=description,
        payload=str(payload, "ascii"),
        provider_token="",
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
    # check the payload, is this from your bot? will raise if not
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
        raise ValueError(f"Invoice with ID {invoice_id} not found")

    if (_tg_user := update.effective_user) is None:
        raise ValueError("telegram user is None")
    # check if invoice user has changed (e.g. forwarded invoice)
    tg_user = session.get(TelegramUser, _tg_user.id)
    if tg_user is None or tg_user.user is None:
        raise ValueError("(telegram) user not found in database")

    invoice.status = InvoiceStatus.paid
    invoice.subscription.active = True
    invoice.subscription.status = SubscriptionStatus.active

    return BotMessage(
        chat_id=update.message.chat_id,
        text=f"Thank you for your payment! Subscription is active until {invoice.subscription.end_date}",
    )


async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bot_msg = _successful_payment_callback(update, context)
    await bot_msg.send(context.bot)


def referral(update: Update, context: DbSessionContext, token: str) -> BotMessage:
    """Generate BotMessage for start handler with referral token."""
    session = context.db_session

    tg_user = session.get(TelegramUser, update.effective_user.id)
    # check if user already has (past or active) premium subscription
    if tg_user.subscriptions:
        return BotMessage(
            chat_id=update.message.chat_id,
            text="You have already used premium. You are not eligible for a referral.",
        )

    # check if token is valid/active
    stmt = select(TelegramUser).where(TelegramUser.referral_token == token).where(TelegramUser.referral_token_active)
    referred_by_user = session.execute(stmt).scalar_one_or_none()
    if referred_by_user is None:
        return BotMessage(
            chat_id=update.message.chat_id,
            text="😵‍💫 Sorry, the referral token is invalid or expired.",
        )
    tg_user.referred_by = referred_by_user

    # create 14 day trial subscription
    start_date = dt.datetime.now(dt.UTC)
    end_date = start_date + dt.timedelta(days=14)
    subscription = Subscription(
        tg_user=tg_user,
        chat_id=update.effective_chat.id,
        start_date=start_date,
        end_date=end_date,
        status=SubscriptionStatus.active,
        type=SubscriptionType.reffered,
        active=True,
    )
    session.add(subscription)

    user_msg = BotMessage(
        text=(
            "🥳 You have successfully activated your 14 day trial premium subscription"
            f"(ends at {end_date.strftime('%x')})"
        ),
        chat_id=update.effective_chat.id,
    )
    admin_group_msg = AdminChannelMessage(
        text=f"New user {tg_user.username or tg_user.first_name} activated 14 day trial premium subscription"
    )

    return [user_msg, admin_group_msg]
