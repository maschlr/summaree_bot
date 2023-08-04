import binascii
import json
from typing import cast

from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update

from ..integrations import TokenEmail, is_valid_email
from ..logging import getLogger
from ..models import EmailToken, Language, TelegramChat, TelegramUser, User
from ..models.session import DbSessionContext
from ..utils import url
from .db import add_session, ensure_chat

# Enable logging
_logger = getLogger(__name__)

__all__ = [
    "start",
    "set_lang",
    "register",
    "send_token_email",
    "edit_email",
    "activate",
    "catch_all",
]

MSG = (
    "Send me a voice message and I will summarize it for you. "
    "You can forward messages from other chats to me, even if they are in other apps."
)


@add_session
@ensure_chat
async def set_lang(update: Update, context: DbSessionContext) -> None:
    """Set the target language when /lang {language_code} is issued."""
    if update.message is None or update.effective_chat is None:
        raise ValueError("The update must contain a message.")

    session = context.db_session
    stmt = select(Language)
    languages = session.scalars(stmt).all()

    def msg(prefix, target_languages=languages):
        return prefix + "\n\t".join(f"{lang.ietf_tag} ({lang.name})" for lang in target_languages)

    try:
        if context.args is None:
            raise IndexError
        target_language_ietf_tag = context.args[0].lower()
        stmt = select(Language).where(Language.ietf_tag == target_language_ietf_tag)
        if target_language := session.scalar(stmt):
            if not (chat := session.get(TelegramChat, update.effective_chat.id)):
                return
            if chat.language != target_language:
                chat.language = target_language
                session.commit()
                await update.message.reply_html(
                    f"Target language successfully set to: {target_language_ietf_tag} ({target_language.name})",
                )
            else:
                other_available_languages_stmt = select(Language).where(Language.ietf_tag != target_language_ietf_tag)
                other_available_languages = session.scalars(other_available_languages_stmt).all()
                answer = (
                    "This language is already configured as the target language: "
                    f"{chat.language.ietf_tag} ({chat.language.name})\n"
                    "Other available languages are:\n\n"
                )

                await update.message.reply_html(
                    msg(answer, other_available_languages),
                )
        else:
            prefix = (
                "Unknown target language. Set your target language with `/lang language`.\n"
                "Available laguages are:\n\n"
            )
            await update.message.reply_html(
                msg(prefix),
            )
    except IndexError:
        await update.message.reply_html(
            msg("Set your target language with `/lang language`. Available languages are: \n\n"),
        )


@add_session
@ensure_chat
async def start(update: Update, context: DbSessionContext) -> None:
    if update.message is None or update.effective_user is None:
        raise ValueError("The update must contain a message and a user.")

    fnc_mapping = {
        # "ref": referral,
        "activate": activate
    }
    if context is not None and context.args is not None:
        try:
            [b64_data] = context.args
            callback_data = cast(list, url.decode(b64_data))
            fnc_key, *args = callback_data
            fnc = fnc_mapping[fnc_key]
            await fnc(update, context, *args)
            return
        except (ValueError, KeyError, binascii.Error, json.JSONDecodeError):
            await update.message.reply_markdown_v2(
                rf"😵‍💫 Receiced invalid argument\(s\) \(`{context.args}`\)",
            )
            raise
    user = update.effective_user
    await update.message.reply_html(
        rf"Hi {user.mention_html()}! " + MSG,
    )


@add_session
@ensure_chat
async def register(update: Update, context: DbSessionContext) -> None:
    if update.message is None or context.args is None or update.effective_user is None:
        _logger.warning("The update must contain a message.")
        return
    try:
        [email_address] = context.args
    except ValueError:
        await update.message.reply_markdown_v2(
            r"""Command usage:
    `/register \<email_address\>`
            """
        )
        return

    if not is_valid_email(email_address):
        await update.message.reply_markdown_v2(
            f"""⚠️ The message you've entered doesn't look like a valid email address (`{email_address}`)"""
        )
        return

    session = context.db_session
    tg_user = session.get(TelegramUser, update.effective_user.id)
    if tg_user is None:
        return
    elif tg_user.user is not None:
        new_email = tg_user.user.email is not None and tg_user.user.email != email_address
        msg = f"""⚠️ You've already registered with the email address `{tg_user.user.email}`\. """
        if new_email:
            edit_callback_data = {"fnc": "edit_email", "args": [email_address]}
            keyboard = [
                [
                    InlineKeyboardButton("✏️ Edit email", callback_data=edit_callback_data),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_markdown_v2(
                msg + rf"Would you like to change your email to `{email_address}`?",
                reply_markup=reply_markup,
            )
            return
        elif tg_user.user.email and not tg_user.user.email_token.active:
            # email address equal to the one already registered
            resend_callback_data = {
                "fnc": "resend_email",
            }
            keyboard = [
                [
                    InlineKeyboardButton("🔁 Re-send token email", callback_data=resend_callback_data),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_markdown_v2(
                msg,
                reply_markup=reply_markup,
            )
            return
        elif tg_user.user.email and tg_user.user.email_token.active and not new_email:
            await update.message.reply_markdown_v2(r"Your email is already activated\. Everything is fine\. 😊")
            return

    if not tg_user.user:
        user = User(telegram_user=tg_user, email=email_address, email_token=EmailToken())
        session.add(user)
    else:
        tg_user.user.email = email_address
        tg_user.user.email_token = EmailToken()
    session.flush()

    await send_token_email(update, context)


@add_session
async def send_token_email(update: Update, context: DbSessionContext) -> None:
    if update.effective_user is None or update.effective_message is None:
        raise ValueError("The update must contain a user and a message.")

    session = context.db_session
    tg_user = session.get(TelegramUser, update.effective_user.id)
    if tg_user is None or tg_user.user is None:
        raise ValueError(f"Telegram user with id {update.effective_user.id} not found.")
    data = {
        "subject": "Activate your summar.ee bot account now",
        "token": tg_user.user.email_token.value,
        "bot_name": context.bot.name[1:],  # remove leading @
        "name": tg_user.first_name,
    }
    email = TokenEmail(template_data=data, email_to=tg_user.user.email)
    success = email.send()
    if reply_markup := update.effective_message.reply_markup:
        await update.effective_message.edit_reply_markup(reply_markup=None)
    else:
        reply_markup = None

    if success:
        await update.effective_message.reply_markdown_v2(
            r"📬 Email sent\! Please check your inbox and activate your account\."
        )
    else:
        msg = r"😵‍💫 Something went wrong. Please try again later\."
        if reply_markup is not None:
            await update.effective_message.reply_markdown_v2(msg, reply_markup=reply_markup)
        else:
            await update.effective_message.reply_markdown_v2(msg)


@add_session
async def edit_email(update: Update, context: DbSessionContext, email: str) -> None:
    if update.effective_user is None or update.effective_message is None:
        raise ValueError("The update must contain a user and a message.")

    session = context.db_session
    tg_user = session.get(TelegramUser, update.effective_user.id)
    if tg_user is None or tg_user.user is None:
        raise ValueError(f"Telegram user with id {update.effective_user.id} not found.")

    tg_user.user.email = email
    token = EmailToken(user=tg_user.user)
    session.add(token)
    session.flush()

    await send_token_email(update, context)
    return


@add_session
@ensure_chat
async def activate(update: Update, context: DbSessionContext) -> None:
    if update.effective_user is None or update.message is None:
        raise ValueError("The update must contain a user.")
    elif context.args is None or len(context.args) != 1:
        await update.message.reply_markdown_v2(
            r"""Command usage:
    `/activate \<token\>`
        """
        )
        return

    session = context.db_session
    tg_user = session.get(TelegramUser, update.effective_user.id)
    if tg_user is None:
        return

    # case 1: no email registered -> show help message
    if not tg_user.user:
        msg = r"""You haven't registered yet. Please use the `/register \<email\>` command to register\."""
        await update.message.reply_markdown_v2(msg)
        return

    stmt = select(EmailToken).where(EmailToken.value == context.args[0])
    token = session.execute(stmt).scalar_one_or_none()
    # case 2: token not found -> show help message
    # case 3: token does not belong to user
    if token is None or token.user.telegram_user_id != tg_user.id:
        msg = r"""⚠️ The token you've entered is invalid. Please check your inbox and try again."""
        await update.message.reply_markdown_v2(msg)
        return
    # case 3: email registered but not activated -> activate
    else:
        token.active = True
        # TODO: add 10 day free premium trial
        await update.message.reply_markdown_v2(r"""✅ Your account has been activated\! 🚀""")
        return


@add_session
@ensure_chat
async def catch_all(update: Update, context: DbSessionContext) -> None:
    if update.message is None:
        raise ValueError("The update must contain a message.")
    else:
        return
        # await update.message.reply_text(MSG)
