import asyncio
import datetime as dt
import json
import os
from datetime import datetime, timedelta
from typing import Mapping, Optional, Sequence, Union, cast

from sqlalchemy import select
from sqlalchemy.orm import Session
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
from ..templates import get_template
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
    """Handler for listing the referrals (hidden from commands menu)"""
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
        md_link_to_stars = {}
        for referred_user in tg_user.referrals:
            amount_stars_from_user = sum(
                invoice.total_amount
                for invoice in referred_user.invoices
                if invoice.product.currency == "XTR" and invoice.status == InvoiceStatus.paid
            )
            md_link_to_stars[referred_user.md_link] = amount_stars_from_user
            n_stars += amount_stars_from_user

        return BotMessage(
            chat_id=chat_id,
            text="\n".join(
                [
                    f"👥 Your referral token url is {tg_user.referral_url}\n",
                    f"💫 You have referred {n_referrals} users. They have paid a total of {n_stars} ⭐:",
                    ("\n".join(f"- {md_link} paid {stars} ⭐" for md_link, stars in md_link_to_stars.items())),
                ]
            ),
            parse_mode=ParseMode.MARKDOWN,
        )


async def referral_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Async handler for listing the referrals"""
    bot_msg = _referral_handler(update, context)
    await bot_msg.send(context.bot)


@session_context
def get_subscription_keyboard(
    update: Update,
    context: DbSessionContext,
    subscription_id: Optional[int] = None,
    return_products: bool = False,
    ietf_tag: str = "en",
) -> Union[InlineKeyboardMarkup, tuple[InlineKeyboardMarkup, Mapping[PremiumPeriod, Product]]]:
    """Returns an InlineKeyboardMarkup with the subscription options."""

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

    lang_to_period_words = {
        "en": {
            PremiumPeriod.MONTH: "month",
            PremiumPeriod.QUARTER: "months",
            PremiumPeriod.YEAR: "year",
        },
        "ru": {
            PremiumPeriod.MONTH: "месяц",
            PremiumPeriod.QUARTER: "месяца",
            PremiumPeriod.YEAR: "год",
        },
        "de": {
            PremiumPeriod.MONTH: "Monat",
            PremiumPeriod.QUARTER: "Monate",
            PremiumPeriod.YEAR: "Jahr",
        },
        "es": {
            PremiumPeriod.MONTH: "mes",
            PremiumPeriod.QUARTER: "meses",
            PremiumPeriod.YEAR: "año",
        },
    }
    lookup = lang_to_period_words.get(ietf_tag, lang_to_period_words["en"])
    # create keyboard
    period_to_keyboard_button_text = {
        PremiumPeriod.MONTH: (
            f"🤖 1 {lookup[PremiumPeriod.MONTH]}: ⭐{periods_to_products[PremiumPeriod.MONTH].discounted_price}"
        ),
        PremiumPeriod.QUARTER: (
            f"💯 3 {lookup[PremiumPeriod.QUARTER]}: ⭐{periods_to_products[PremiumPeriod.QUARTER].discounted_price}"
        ),
        PremiumPeriod.YEAR: (
            f"🔥 1 {lookup[PremiumPeriod.YEAR]}: ⭐{periods_to_products[PremiumPeriod.YEAR].discounted_price}"
        ),
    }
    keyboard_buttons = [
        [
            InlineKeyboardButton(
                text,
                callback_data=dict(
                    **callback_data,
                    kwargs={"product_id": periods_to_products[period].id},
                ),
            )
        ]
        for period, text in period_to_keyboard_button_text.items()
    ]
    lang_to_remove_button_text = {
        "en": "😌 No, thanks",
        "ru": "😌 Нет, спасибо",
        "de": "😌 Nein, danke",
        "es": "😌 No, gracias",
    }
    remove_button_text = lang_to_remove_button_text.get(ietf_tag, lang_to_remove_button_text["en"])
    keyboard_buttons.append([InlineKeyboardButton(remove_button_text, callback_data={"fnc": "remove_inline_keyboard"})])
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
        reply_markup, periods_to_products = get_subscription_keyboard(
            update, context, subscription_id=subscriptions[0].id, return_products=True
        )
        template = get_template("premium_active", update)
        text = template.render(subscriptions=subscriptions, periods_to_products=periods_to_products)
        return BotMessage(
            chat_id=update.effective_chat.id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    # case 2: chat has no active subscription
    #  -> check if user is in database, if not -> create
    #  -> ask user if subscription should be bought
    else:
        reply_markup, periods_to_products = get_subscription_keyboard(update, context, return_products=True)
        template = get_template("premium_inactive", update)
        text = template.render(periods_to_products=periods_to_products)
        return BotMessage(
            chat_id=update.effective_chat.id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN_V2,
        )


def get_sale_text(periods_to_products: Mapping[PremiumPeriod, Product], update: Optional[Update] = None) -> str:
    template = get_template("sale_suffix", update)
    return template.render(periods_to_products=periods_to_products)


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
    days = product.premium_period.value
    end_date = start_date + timedelta(days=days)
    subscription = create_subscription(
        session,
        tg_user_id=tg_user.id,
        duration=product.premium_period.value,
        chat_id=chat_id,
        start_date=start_date,
    )
    lang_to_title = {
        "en": "summar.ee premium",
        "ru": "summar.ee премиум",
        "de": "summar.ee Premium",
        "es": "summar.ee premium",
    }
    title = lang_to_title.get(update.effective_user.language_code, lang_to_title["en"])
    # In order to get a provider_token see https://core.telegram.org/bots/payments#getting-a-token
    currency = "XTR"
    price = product.discounted_price
    lang_to_description = {
        "en": (
            f"Premium features for {days} days (from {start_date.strftime('%x')} to {end_date.strftime('%x')}; "
            "ends automatically)"
        ),
        "ru": (
            f"Премиум-функции на {days} дней (с {start_date.strftime('%x')} по {end_date.strftime('%x')}; "
            "автоматически заканчивается)"
        ),
        "de": (
            f"Premium-Funktionen für {days} Tage (von {start_date.strftime('%x')} bis {end_date.strftime('%x')}; "
            "endet automatisch)"
        ),
        "es": (
            f"Premium por {days} días (desde {start_date.strftime('%x')} hasta {end_date.strftime('%x')}; "
            "se termina automáticamente)"
        ),
    }
    description = lang_to_description.get(update.effective_user.language_code, lang_to_description["en"])
    prices = [LabeledPrice(description, price)]

    invoice = Invoice(
        tg_user_id=tg_user.id,
        chat_id=chat_id,
        product_id=product.id,
        subscription=subscription,
    )
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


def create_subscription(
    session: Session,
    tg_user_id: int,
    duration: int,
    chat_id: Optional[int] = None,
    start_date: Optional[datetime] = None,
    to_be_paid: bool = True,
) -> int:
    tg_user = session.get(TelegramUser, tg_user_id)
    if tg_user is None:
        raise ValueError(f"User {tg_user_id} not found")
    if chat_id is None:
        chat_id = tg_user_id
    if start_date is None:
        start_date = datetime.now(dt.UTC)
    end_date = start_date + timedelta(days=duration)

    if to_be_paid:
        sub_kwargs = dict(
            status=SubscriptionStatus.pending,
            type=SubscriptionType.paid,
            active=False,
        )
    else:
        sub_kwargs = dict(
            status=SubscriptionStatus.active,
            type=SubscriptionType.reffered,
            active=True,
        )

    subscription = Subscription(
        tg_user=tg_user, chat_id=chat_id, start_date=start_date, end_date=end_date, **sub_kwargs
    )
    session.add(subscription)
    session.flush()
    return subscription


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
            ok=False,
            error_message="😕 Something went wrong... Invoice has been cancelled. Support has been contacted.",
        )
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
        chat_id=update.effective_chat.id,
        text=f"Thank you for your payment! Subscription is active until {invoice.subscription.end_date}",
    )


async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bot_msg = _successful_payment_callback(update, context)
    new_invoice_msg = AdminChannelMessage(
        text=f"💸 Winner, winner, chicken dinner! {update.effective_user.mention_markdown_v2} just paid an invoice!",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    async with asyncio.TaskGroup() as tg:
        tg.create_task(bot_msg.send(context.bot))
        tg.create_task(new_invoice_msg.send(context.bot))


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

    lang_to_text = {
        "en": (
            "🥳 You have successfully activated your 14 day trial premium features"
            f" (ends on {end_date.strftime('%x')})"
        ),
        "ru": (
            "🥳 Вы успешно активировали 14-дневную пробную версию премиум-функций"
            f" (заканчивается {end_date.strftime('%x')})"
        ),
        "de": (
            "🥳 Du hast deine 14-tägige Testversion der Premium-Funktionen erfolgreich aktiviert"
            f" (endet am {end_date.strftime('%x')})"
        ),
        "es": (
            "🥳 Has activado con éxito tu prueba de 14 días de las funciones premium"
            f" (termina el {end_date.strftime('%x')})"
        ),
    }
    text = lang_to_text.get(update.effective_user.language_code, lang_to_text["en"])
    user_msg = BotMessage(
        text=text,
        chat_id=update.effective_chat.id,
    )
    admin_group_msg = AdminChannelMessage(
        text=f"User {tg_user.username or tg_user.first_name} activated 14 day trial premium subscription"
    )

    return [user_msg, admin_group_msg]
