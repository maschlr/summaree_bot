import asyncio
import binascii
import json

try:
    from itertools import batched
except ImportError:
    from itertools import islice

    def batched(iterable, n):
        # batched('ABCDEFG', 3) → ABC DEF G
        if n < 1:
            raise ValueError("n must be at least one")
        iterator = iter(iterable)
        while batch := tuple(islice(iterator, n)):
            yield batch


from typing import Callable, Optional, Sequence, Union, cast

from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ..integrations import TokenEmail, is_valid_email
from ..logging import getLogger
from ..models import EmailToken, Language, TelegramChat, TelegramUser, User
from ..models.session import DbSessionContext
from ..utils import url
from . import BotMessage
from .db import ensure_chat, session_context
from .helpers import escape_markdown

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


@session_context
@ensure_chat
def _set_lang(update: Update, context: DbSessionContext) -> BotMessage:
    """Set the target language when /lang {language_code} is issued."""
    if update.effective_chat is None:
        raise ValueError("The update must contain a message.")

    session = context.db_session
    if session is None:
        raise ValueError("The context must contain a database session.")

    stmt = select(Language)
    languages = session.scalars(stmt).all()
    if not languages:
        raise ValueError("No languages found in database.")

    example_suffix = "\n".join(
        [
            "Example for English type: `/lang en`",
            "Para Español escribe `/lang es`",
            "Для Русского напишите `/lang ru`\n",
            "Or choose press a button below:",
        ]
    )

    def msg(prefix: str, target_languages: Sequence[Language] = languages, suffix: str = "") -> str:
        _msg = "".join(
            [
                prefix,
                "\n".join([f"{lang.flag_emoji} {lang.ietf_tag} [{lang.name}]" for lang in target_languages]),
                f"\n\n{suffix}" if len(suffix) > 0 else suffix,
            ]
        )
        return escape_markdown(_msg)

    parse_mode = ParseMode.MARKDOWN_V2

    chat = session.get(TelegramChat, update.effective_chat.id)
    if chat is None:
        raise ValueError(f"Could not find chat with id {update.effective_chat.id}")
    try:
        if context.args is None:
            raise IndexError
        target_language_ietf_tag = context.args[0].lower()
        stmt = select(Language).where(Language.ietf_tag == target_language_ietf_tag)
        if target_language := session.scalar(stmt):
            if chat.language != target_language:
                chat.language = target_language
                _msg = "".join(
                    [
                        "Language successfully set to: ",
                        f"{target_language.flag_emoji} {target_language_ietf_tag} [{target_language.name}]",
                    ]
                )
                return BotMessage(
                    chat_id=chat.id,
                    text=msg(
                        _msg,
                        [],
                    ),
                    parse_mode=parse_mode,
                )
            else:
                other_available_languages_stmt = select(Language).where(Language.ietf_tag != target_language_ietf_tag)
                other_available_languages = session.scalars(other_available_languages_stmt).all()
                answer = (
                    "This language is already configured as the target language: "
                    f"{chat.language.flag_emoji} {chat.language.ietf_tag} [{chat.language.name}]\n"
                    "Other available languages are:\n\n"
                )

                return BotMessage(
                    chat_id=chat.id,
                    text=msg(answer, other_available_languages, example_suffix),
                    parse_mode=parse_mode,
                )

        else:
            prefix = "Unknown language.\n" "Available languages are:\n\n"
            return BotMessage(
                chat_id=chat.id,
                text=msg(prefix, languages, example_suffix),
                parse_mode=parse_mode,
            )

    except IndexError:
        # Give the user an inline keyboard to choose from
        # make the inline keyboard pageable
        # first page has only "Next >>" button to go to the next page, last page only "<< Previous" button
        # Give the user 4 options, not the one they already have
        reply_markup = _get_lang_inline_keyboard(update, context)
        return BotMessage(
            chat_id=chat.id,
            text=msg(
                (
                    "Current language is: "
                    f"{chat.language.flag_emoji} {chat.language.ietf_tag} [{chat.language.name}]\n\n"
                    "Your can either choose one of the languages below or "
                    "set your target language with `/lang` followed by the "
                    "language short code from the following list.\n\n"
                ),
                languages,
                example_suffix,
            ),
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )


@session_context
def _get_lang_inline_keyboard(update: Update, context: DbSessionContext, page: int = 1) -> InlineKeyboardMarkup:
    """
    Give the user an inline keyboard to choose from
    make the inline keyboard pageable
    first page has only "Next >>" button to go to the next page, last page only "<< Previous" button
    Give the user 4 options, not the one they already have
    """
    session = context.db_session
    if session is None:
        raise ValueError("The context must contain a database session.")

    # Possible future performance improvement: cache the languages
    stmt = select(Language)
    languages = session.scalars(stmt).all()
    chat = session.get(TelegramChat, update.effective_chat.id)

    # define rows and columns for the keyboard
    rows = 4
    columns = 3

    common_languages_ietf_tag = ["en", "ru", "pt", "zh", "es", "fr"]
    all_languages_without_common = [lang for lang in languages if lang.ietf_tag not in common_languages_ietf_tag]
    all_languages_ietf_tag = common_languages_ietf_tag + [lang.ietf_tag for lang in all_languages_without_common]

    # remove the language that is already configured
    all_languages_ietf_tag.remove(chat.language.ietf_tag)

    # sorted languages with most common first
    ietf_tag_to_language = {lang.ietf_tag: lang for lang in languages}
    sorted_languages = [ietf_tag_to_language[ietf_tag] for ietf_tag in all_languages_ietf_tag]

    callback_data = {"fnc": "set_lang"}
    language_buttons = [
        InlineKeyboardButton(
            f"{lang.flag_emoji} {lang.name}",
            callback_data=dict(**callback_data, kwargs={"ietf_tag": lang.ietf_tag}),
        )
        for lang in sorted_languages
    ]

    n_lang = len(all_languages_ietf_tag)
    items_per_page = rows * columns
    n_pages = int((n_lang - 2) / (items_per_page - 2)) + 1

    # indexing of pages starts with 1
    if page == 1:
        buttons_on_page = language_buttons[: items_per_page - 1]
        # only next button at last position of last row
        buttons_on_page.append(InlineKeyboardButton("Next >>", callback_data=dict(**callback_data, kwargs={"page": 2})))
    elif page == n_pages:
        # last page
        # only previous button at first position of first row
        # calculate the number of pages
        items_total = n_lang + 2 + 2 * (n_pages - 2)

        # slots on n-1 pages
        slots = (n_pages - 1) * items_per_page
        items_on_last_page = items_total - slots

        buttons_on_page = language_buttons[-items_on_last_page + 1 :]
        buttons_on_page.insert(
            0, InlineKeyboardButton("<< Previous", callback_data=dict(**callback_data, kwargs={"page": page - 1}))
        )
    else:
        # middle page
        _start = items_per_page - 1 + (page - 2) * (items_per_page - 2)
        buttons_on_page = language_buttons[_start : _start + items_per_page - 2]
        buttons_on_page.insert(
            0, InlineKeyboardButton("<< Previous", callback_data=dict(**callback_data, kwargs={"page": page - 1}))
        )
        buttons_on_page.append(
            InlineKeyboardButton("Next >>", callback_data=dict(**callback_data, kwargs={"page": page + 1}))
        )

    keyboard = InlineKeyboardMarkup(list(batched(buttons_on_page, columns)))
    return keyboard


async def set_lang_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, ietf_tag=None, page=None) -> None:
    if ietf_tag is not None:
        context.args = [ietf_tag]
        await set_lang(update, context)
    elif page is not None:
        keyboard = _get_lang_inline_keyboard(update, context, page=page)
        await update.callback_query.edit_message_reply_markup(reply_markup=keyboard)
    else:
        raise ValueError("Invalid callback data.")


async def set_lang(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set the target language when /lang {language_code} is issued."""
    bot_msg = _set_lang(update, context)
    await bot_msg.send(context.bot)


@session_context
@ensure_chat
def _start(update: Update, context: DbSessionContext) -> Union[Callable, BotMessage]:
    if update.message is None or update.effective_user is None:
        raise ValueError("The update must contain a message and a user.")

    fnc_mapping = {
        # TODO "ref": referral,
        "activate": activate
    }
    if context is not None and context.args is not None and len(context.args):
        [b64_data] = context.args
        callback_data = cast(Sequence, url.decode(b64_data))
        fnc_key, *args = callback_data
        fnc = fnc_mapping[fnc_key]
        return lambda: fnc(update, context, *args)

    user = update.effective_user
    bot_msg = BotMessage(
        chat_id=update.message.chat_id,
        text=f"Hi {user.mention_html()}! " + MSG,
        parse_mode=ParseMode.HTML,
    )
    return bot_msg


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start the bot."""
    if update.message is None:
        raise ValueError("The update must contain a message.")

    try:
        result = _start(update, context)
    except (
        ValueError,
        KeyError,
        binascii.Error,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ):
        _logger.warning("Received invalid start handler argument(s) (%s)", context.args)
        bot_msg = BotMessage(
            chat_id=update.message.chat_id,
            text=escape_markdown(f"😵‍💫 Receiced invalid argument(s) (`{context.args}`)"),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        await bot_msg.send(context.bot)
        raise

    if isinstance(result, BotMessage):
        await result.send(context.bot)
        await help_handler(update, context)
    else:
        await result()


async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show help message."""

    if update.message is None:
        raise ValueError("The update must contain a message.")

    commmands = await context.bot.get_my_commands()
    bot_msg = BotMessage(
        chat_id=update.message.chat_id,
        text="Available commands are:\n"
        + "\n".join(f"/{command.command} - {command.description}" for command in commmands),
    )
    await bot_msg.send(context.bot)


async def register(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with asyncio.TaskGroup() as tg:
        for operation in _register(update, context):
            if isinstance(operation, BotMessage):
                tg.create_task(operation.send(context.bot))
            elif isinstance(operation, Callable):
                tg.create_task(operation())
            else:
                raise ValueError(f"Invalid operation: {operation}")


@session_context
@ensure_chat
def _register(update: Update, context: DbSessionContext) -> Sequence[Union[BotMessage, Callable]]:
    if update.message is None or context.args is None or update.effective_user is None:
        raise ValueError("The update must contain a message.")

    try:
        [email_address] = context.args
    except ValueError:
        return [
            BotMessage(
                chat_id=update.message.chat_id,
                text=r"""Command usage:
    `/register \<email_address\>`
            """,
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        ]
    if not is_valid_email(email_address):
        return [
            BotMessage(
                chat_id=update.message.chat_id,
                text=f"""⚠️ The message you've entered doesn't look like a valid email address (`{email_address}`)""",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        ]

    session = context.db_session
    tg_user = session.get(TelegramUser, update.effective_user.id)
    if tg_user is None:
        raise ValueError(f"Could not find Telegram user with id {update.effective_user.id}")
    elif tg_user.user is not None:
        new_email = tg_user.user.email is not None and tg_user.user.email != email_address
        msg = f"""⚠️ You've already registered with the email address `{tg_user.user.email}`\. """
        if new_email:
            edit_email_callback_data = {"fnc": "edit_email", "args": [email_address]}
            keyboard = [
                [
                    InlineKeyboardButton("✏️ Edit email", callback_data=edit_email_callback_data),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            return [
                BotMessage(
                    chat_id=update.message.chat_id,
                    text=msg + rf"Would you like to change your email to `{email_address}`?",
                    parse_mode=ParseMode.MARKDOWN_V2,
                    reply_markup=reply_markup,
                )
            ]
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
            return [
                BotMessage(
                    chat_id=update.message.chat_id,
                    text=msg,
                    parse_mode=ParseMode.MARKDOWN_V2,
                    reply_markup=reply_markup,
                )
            ]
        elif tg_user.user.email and tg_user.user.email_token.active and not new_email:
            return [
                BotMessage(
                    chat_id=update.message.chat_id,
                    text="Your email is already activated. Everything is fine. 😊",
                )
            ]

    # new user
    if not tg_user.user:
        user = User(telegram_user=tg_user, email=email_address, email_token=EmailToken())
        session.add(user)
    else:  # exiting user, new email
        tg_user.user.email = email_address
        tg_user.user.email_token = EmailToken()
    session.flush()

    return _send_token_email(update, context)


@session_context
def _send_token_email(update: Update, context: DbSessionContext) -> Sequence[Union[BotMessage, Callable]]:
    message, chat, user = (
        update.effective_message,
        update.effective_chat,
        update.effective_user,
    )
    if message is None or chat is None or user is None:
        raise ValueError("The update must contain a user and a message.")

    operations: list[Union[BotMessage, Callable]] = []

    session = context.db_session
    tg_user = session.get(TelegramUser, user.id)
    if tg_user is None or tg_user.user is None:
        raise ValueError(f"Telegram user with id {user.id} not found.")
    data = {
        "subject": "Activate your summar.ee bot account now",
        "token": tg_user.user.email_token.value,
        "bot_name": context.bot.name[1:],  # remove leading @
        "name": tg_user.first_name,
    }
    email = TokenEmail(template_data=data, email_to=tg_user.user.email)
    success = email.send()
    if (reply_markup := message.reply_markup) and (edit_markup := message.edit_reply_markup) is not None:
        operations.append(lambda: edit_markup(reply_markup=None))
    else:
        reply_markup = None

    if success:
        operations.append(
            BotMessage(
                chat_id=chat.id,
                text="📬 Email sent! Please check your inbox and activate your account.",
            )
        )
    else:
        operations.append(
            BotMessage(
                chat_id=chat.id,
                text="😵‍💫 Something went wrong. Please try again later.",
                reply_markup=reply_markup,
            )
        )

    return operations


async def send_token_email(update: Update, context: DbSessionContext) -> None:
    async with asyncio.TaskGroup() as tg:
        operations = _send_token_email(update, context)
        for operation in operations:
            if isinstance(operation, BotMessage):
                tg.create_task(operation.send(context.bot))
            else:
                tg.create_task(operation())


@session_context
def _edit_email(update: Update, context: DbSessionContext, email: str) -> Sequence[Union[BotMessage, Callable]]:
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

    return _send_token_email(update, context)


async def edit_email(update: Update, context: DbSessionContext, email: str) -> None:
    async with asyncio.TaskGroup() as tg:
        for op in _edit_email(update, context, email):
            if isinstance(op, BotMessage):
                tg.create_task(op.send(context.bot))
            else:
                tg.create_task(op())


@session_context
@ensure_chat
def _activate(update: Update, context: DbSessionContext) -> BotMessage:
    if update.effective_user is None or update.message is None:
        raise ValueError("The update must contain a user.")
    elif context.args is None or len(context.args) != 1:
        bot_msg = BotMessage(
            chat_id=update.message.chat_id,
            text=r"""Command usage:
    `/activate \<token\>`
        """,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return bot_msg

    session = context.db_session
    tg_user = session.get(TelegramUser, update.effective_user.id)
    if tg_user is None:
        raise ValueError(f"Telegram user with id {update.effective_user.id} not found.")

    # case 1: no email registered -> show help message
    if not tg_user.user:
        msg = r"""You haven't registered yet. Please use the `/register \<email\>` command to register\."""
        return BotMessage(chat_id=update.message.chat_id, text=msg, parse_mode=ParseMode.MARKDOWN_V2)

    stmt = select(EmailToken).where(EmailToken.value == context.args[0])
    token = session.execute(stmt).scalar_one_or_none()
    # case 2: token not found -> show help message
    # case 3: token does not belong to user
    if token is None or token.user.telegram_user_id != tg_user.id:
        msg = r"""⚠️ The token you've entered is invalid. Please check your inbox and try again."""
        return BotMessage(chat_id=update.message.chat_id, text=msg, parse_mode=ParseMode.MARKDOWN_V2)

    # case 3: email registered but not activated -> activate
    else:
        token.active = True
        # TODO: add 10 day free premium trial
        return BotMessage(
            chat_id=update.message.chat_id,
            text=r"""✅ Your account has been activated\! 🚀""",
            parse_mode=ParseMode.MARKDOWN_V2,
        )


async def activate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bot_msg = _activate(update, context)
    await bot_msg.send(context.bot)


@session_context
@ensure_chat
def _catch_all(update: Update, context: DbSessionContext) -> Optional[BotMessage]:
    if update.message is None:
        raise ValueError("The update must contain a message.")
    else:
        return None


async def catch_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if bot_msg := _catch_all(update, context):
        await bot_msg.send(context.bot)
