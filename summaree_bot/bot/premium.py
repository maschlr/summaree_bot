import asyncio
import datetime as dt
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Mapping, Optional, Sequence, Union, cast

from sqlalchemy import extract, select
from sqlalchemy.orm import Session
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown

from ..models import (
    Invoice,
    InvoiceStatus,
    PremiumPeriod,
    Product,
    Subscription,
    SubscriptionStatus,
    SubscriptionType,
    Summary,
    TelegramChat,
    TelegramUser,
)
from ..models.session import DbSessionContext
from ..models.session import Session as SessionMaker
from ..templates import get_template
from ..utils import url
from . import AdminChannelMessage, BotInvoice, BotMessage
from .db import ensure_chat, session_context
from .exceptions import NoActivePremium

__all__ = [
    "premium_handler",
    "precheckout_callback",
    "payment_callback",
    "referral_handler",
    "successful_payment_callback",
    "get_sale_text",
]

_logger = logging.getLogger(__name__)
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
        return BotMessage(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_to_message_id=update.effective_message.id,
        )
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
            reply_to_message_id=update.effective_message.id,
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
) -> Union[InlineKeyboardMarkup, tuple[InlineKeyboardMarkup, Mapping[PremiumPeriod, Product]]]:
    """Returns an InlineKeyboardMarkup with the subscription options."""

    callback_data: dict[str, Union[str, Sequence, Mapping]] = {"fnc": "buy_or_extend_subscription"}
    ietf_tag = update.effective_user.language_code

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
                    kwargs={"product_id": periods_to_products[period].id, "subscription_id": subscription_id},
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
            reply_to_message_id=update.effective_message.id,
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
            reply_to_message_id=update.effective_message.id,
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
def _payment_callback(
    update: Update, context: DbSessionContext, product_id: int, subscription_id: Optional[int]
) -> BotInvoice:
    """Sends an invoice without shipping-payment."""
    if update.effective_chat is None or update.effective_user is None:
        raise ValueError("chat/user is None")
    session = context.db_session
    product = session.get(Product, product_id)
    if product is None:
        raise ValueError(f"product {product_id} not found in database")

    # create subscription
    start_date = datetime.now(dt.UTC)
    days = product.premium_period.value
    end_date = start_date + timedelta(days=days)
    # TODO: handle case when subscription is extended
    chat_id = update.effective_chat.id
    subscription = create_subscription(
        update=update,
        session=session,
        chat_id=chat_id,
        duration=product.premium_period.value,
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

    # create invoice
    invoice = Invoice(
        tg_user_id=update.effective_user.id,
        chat_id=chat_id,
        product_id=product.id,
        subscription=subscription,
        total_amount=price,
        currency=currency,
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
    update: Update,
    session: Session,
    chat_id: int,
    duration: int,
    start_date: Optional[datetime] = None,
    to_be_paid: bool = True,
) -> Subscription:
    tg_chat = session.get(TelegramChat, chat_id)
    if tg_chat is None:
        raise ValueError(f"Chat {chat_id} not found")
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

    # create subscription
    chat_id = tg_chat.id
    if chat_id > 0:
        # if chat_id is positive, it is a private chat
        tg_user_id = chat_id
    else:
        # if chat_id is negative, it is a group chat
        # -> create a separate subscription for the user
        tg_user_id = update.effective_user.id
        user_subscription = Subscription(
            tg_user_id=tg_user_id, chat_id=tg_user_id, start_date=start_date, end_date=end_date, **sub_kwargs
        )
        session.add(user_subscription)

    subscription = Subscription(
        tg_user_id=tg_user_id, chat_id=chat_id, start_date=start_date, end_date=end_date, **sub_kwargs
    )
    session.add(subscription)
    session.flush()
    return subscription


async def payment_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE, product_id: int, subscription_id: Optional[int] = None
) -> None:
    bot_invoice = _payment_callback(update, context, product_id, subscription_id)
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
    payment = update.message.successful_payment
    if update.message is None or payment is None:
        raise ValueError("update.message.successful_payment is None")

    session = context.db_session
    invoice_id = check_payment_payload(context, payment.invoice_payload)
    # create subscription
    invoice = session.get(Invoice, invoice_id)
    if not invoice:
        raise ValueError(f"Invoice with ID {invoice_id} not found")

    invoice.status = InvoiceStatus.paid
    invoice.subscription.active = True
    invoice.subscription.status = SubscriptionStatus.active
    invoice.provider_payment_charge_id = payment.provider_payment_charge_id
    invoice.telegram_payment_charge_id = payment.telegram_payment_charge_id
    invoice.paid_at = datetime.now(dt.UTC)
    if payment.total_amount != invoice.total_amount:
        invoice.total_amount = payment.total_amount
        _logger.warning(f"Invoice amount {invoice.total_amount} != payment amount {payment.total_amount}")

    end_date_str = invoice.subscription.end_date.strftime("%x")
    lang_to_text = {
        "en": f"Thank you for your payment! Premium is active until {end_date_str}",
        "ru": f"Спасибо за оплату! Премиум-функции активны до {end_date_str}",
        "de": f"Danke für die Zahlung! Premium-Funktionen sind aktiv bis {end_date_str}",
        "es": f"Gracias por su pago! Las funciones premium están activas hasta el {end_date_str}",
    }
    return BotMessage(
        chat_id=update.effective_chat.id,
        text=lang_to_text.get(update.effective_user.language_code, lang_to_text["en"]),
        reply_to_message_id=update.effective_message.id,
    )


async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bot_msg = _successful_payment_callback(update, context)
    new_invoice_msg = AdminChannelMessage(
        text=(
            "💸 Winner, winner, chicken dinner\! "
            f"{update.effective_user.mention_markdown_v2()} just paid an invoice\!"
        ),
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
            reply_to_message_id=update.effective_message.id,
        )

    # check if token is valid/active
    stmt = select(TelegramUser).where(TelegramUser.referral_token == token).where(TelegramUser.referral_token_active)
    referred_by_user = session.execute(stmt).scalar_one_or_none()
    if referred_by_user is None:
        return BotMessage(
            chat_id=update.message.chat_id,
            text="😵‍💫 Sorry, the referral token is invalid or expired.",
            reply_to_message_id=update.effective_message.id,
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
    user_msg = BotMessage(text=text, chat_id=update.effective_chat.id, reply_to_message_id=update.effective_message.id)
    admin_group_msg = AdminChannelMessage(
        text=f"User {tg_user.username or tg_user.first_name} activated 14 day trial premium subscription"
    )

    return [user_msg, admin_group_msg]


async def check_premium_features(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """
    Check if the message needs premium features:
    1. Filesize is larger than 10MB
    2. Current month >= 5 summaries

    Throws a NoActivePremium exception, returns None if all is good and we can proceed
    """
    if (
        update.message is None
        or update.effective_chat is None
        or update.effective_user is None
        or (
            (voice := update.message.voice) is None
            and (audio := update.message.audio) is None
            and (document := update.message.document) is None
            and (video := update.message.video) is None
            and (video_note := update.message.video_note) is None
        )
    ):
        raise ValueError("The update must contain chat/user/voice/audio message.")

    with SessionMaker.begin() as session:
        # check how many transcripts/summaries have already been created in the current month
        chat = session.get(TelegramChat, update.effective_chat.id)
        user = session.get(TelegramUser, update.effective_user.id)

        premium_active = chat.is_premium_active or user.is_premium_active
        if premium_active:
            return None  # No premium features needed, or user has premium subscription

        # send only a message if no user in the chat has an active premium subscription
        any_user_in_chat_is_premium = any(user.is_premium_active for user in chat.users)
        # video messages are a premium feature so we don't check their file size here
        file_size = cast(
            int, voice.file_size if voice else audio.file_size if audio else document.file_size if document else 0
        )
        subscription_keyboard = get_subscription_keyboard(update, context)
        if file_size > 10 * 1024 * 1024 and not premium_active:
            if not any_user_in_chat_is_premium:
                lang_to_text = {
                    "en": r"⚠️ Maximum file size for non\-premium is 10MB\. "
                    r"Please send a smaller file or upgrade to `/premium`\.",
                    "de": r"⚠️ Die maximale Dateigröße für Nicht\-Premium\-Nutzer beträgt 10MB\. "
                    r"Bitte sende eine kleinere Datei oder aktualisiere `/premium`\.",
                    "es": r"⚠️ El tamaño máximo de archivo para no\-premium es de 10MB\. "
                    r"Envíe un archivo más pequeño o actualice a `/premium`\.",
                    "ru": r"⚠️ Максимальный размер файла для не\-премиум составляет 10MB\. "
                    r"Отправьте меньший файл или обновитесь до `/premium`\.",
                }
                text = lang_to_text.get(chat.language.code, lang_to_text["en"])
                await update.message.reply_markdown_v2(
                    text,
                    reply_markup=subscription_keyboard,
                )

            admin_msg = AdminChannelMessage(
                text=(
                    f"User {update.effective_user.mention_markdown_v2()} tried to send "
                    f"a file than was {escape_markdown(f'{file_size / 1024 / 1024:.2f} MB', version=2)}\n"
                    f"(in chat {update.effective_chat.mention_markdown_v2()})"
                ),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            await admin_msg.send(context.bot)
            raise NoActivePremium("File size limit reached for non-premium users")

        current_month = dt.datetime.now(tz=dt.UTC).month
        summaries_this_month = (
            session.query(Summary)
            .filter(
                extract("month", Summary.created_at) == current_month, Summary.tg_chat_id == update.effective_chat.id
            )
            .all()
        )
        if len(summaries_this_month) >= 5 and not premium_active:
            if not any_user_in_chat_is_premium:
                lang_to_text = {
                    "en": r"⚠️ Sorry, you have reached the limit of 5 summaries per month\. "
                    r"Please consider upgrading to `/premium` to get unlimited summaries\.",
                    "de": r"⚠️ Sorry, du hast die Grenze von 5 Zusammenfassungen pro Monat erreicht\. "
                    r"Mit Premium erhälts du eine unbegrenzte Anzahl an Zusammenfassungen\.",
                    "es": r"⚠️ Lo sentimos, has alcanzado el límite de 5 resúmenes al mes\. "
                    r"Considere actualizar a `/premium` para obtener resúmenes ilimitados\.",
                    "ru": r"⚠️ Извините, вы достигли ограничения в 5 резюме в месяц\. "
                    r"Пожалуйста, рассмотрите возможность обновления до `/premium` для"
                    r" получения неограниченных резюме\.",
                }

                text = lang_to_text.get(chat.language.code, lang_to_text["en"])
                await update.effective_message.reply_markdown_v2(
                    text,
                    reply_markup=subscription_keyboard,
                )

            admin_text = (
                f"User {update.effective_user.mention_markdown_v2()} reached the limit of 5 summaries per month\.\n"
                f"`user_id: {update.effective_user.id}`\n"
                f"`chat_id: {update.effective_chat.id}`"
            )
            admin_msg = AdminChannelMessage(
                text=admin_text,
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            await admin_msg.send(context.bot)
            raise NoActivePremium("Monthly message limit reached for non-premium users")

        if video or video_note:
            lang_to_text = {
                "en": "🎥 Video messages are a premium feature. Please upgrade to premium.",
                "de": "🎥 Video Nachrichten sind eine Premium-Funktion. Bitte aktualisiere auf Premium.",
                "es": "🎥 Los mensajes de video son una función premium. Por favor, actualiza a premium.",
                "ru": "🎥 Видеосообщения - это премиум-функция. Пожалуйста, обновитесь до премиум.",
            }
            user_return_text = lang_to_text.get(chat.language.code, lang_to_text["en"])
            await update.effective_message.reply_markdown_v2(
                user_return_text,
                reply_markup=subscription_keyboard,
            )

            admin_message = AdminChannelMessage(
                text=(
                    f"User {update.effective_user.mention_markdown_v2()} tried to send a video message "
                    f"(in chat {update.effective_chat.mention_markdown_v2()})"
                ),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            await admin_message.send(context.bot)
            raise NoActivePremium("Video messages are a premium feature")
