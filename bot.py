import binascii
import json
import os
from functools import wraps
from typing import Any, Callable, cast
from urllib.parse import urlparse

from sqlalchemy import select
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    InvalidCallbackData,
    MessageHandler,
    filters,
)

from summaree_bot.integrations import (
    TokenEmail,
    check_database_languages,
    is_valid_email,
    summarize,
    transcribe_audio,
    transcribe_voice,
    translate,
)
from summaree_bot.logging import getLogger
from summaree_bot.models import Language, TelegramChat, TelegramUser, Token, User
from summaree_bot.models.session import add_session
from summaree_bot.utils import DbSessionContext, url

# Enable logging
_logger = getLogger(__name__)


# TODO register an error handler that always writes a message to myself (the bot owner)

MSG = (
    "Send me a voice message and I will summarize it for you. "
    "You can forward messages from other chats to me, even if they are in other apps."
)


def ensure_chat(fnc):
    @wraps(fnc)
    def wrapper(*args, **kwargs):
        # update is either in kwargs or first arg
        update = kwargs.get("update", args[0])

        context = kwargs.get("context", args[1])
        session = context.db_session

        if not (user := session.get(TelegramUser, update.effective_user.id)):
            attrs = [
                "id",
                "first_name",
                "last_name",
                "username",
                "language_code",
                "is_premium",
                "is_bot",
            ]
            user_kwargs = {attr: getattr(update.effective_user, attr, None) for attr in attrs}

            user = TelegramUser(**user_kwargs)
            session.add(user)

        if not (chat := session.get(TelegramChat, update.effective_chat.id)):
            # standard is english language
            en_lang = Language.get_default_language(session)
            if en_lang is None:
                raise ValueError("English language not found in database.")

            chat = TelegramChat(
                id=update.effective_chat.id,
                type=update.effective_chat.type,
                language=en_lang,
                users={user},
            )
            session.add(chat)
            # TODO: emit welcome message
        elif user not in chat.users:
            chat.users.append(user)

        return fnc(*args, **kwargs)

    return wrapper


# Define a few command handlers. These usually take the two arguments update and
# context.
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
            return
    user = update.effective_user
    await update.message.reply_html(
        rf"Hi {user.mention_html()}! " + MSG,
    )


@add_session
@ensure_chat
async def transcribe_and_summarize(update: Update, context: DbSessionContext) -> None:
    if update.message is None or update.effective_chat is None or update.effective_user is None:
        _logger.warning("The update must contain a voice message.")
        return

    _logger.info(f"Summarizing voice message {update.message.id} from {update.effective_user.name}")
    await update.message.reply_chat_action(action="typing")
    session = context.db_session
    if update.message.voice is not None:
        transcript = transcribe_voice(update, session)
    elif update.message.audio is not None:
        transcript = transcribe_audio(update, session)
    else:
        raise ValueError("The update must contain a voice or audio message.")

    summary = summarize(transcript=await transcript, session=session)
    chat = session.get(TelegramChat, update.effective_chat.id)
    if chat is None:
        return
    en_lang = Language.get_default_language(session)
    if chat.language != en_lang:
        translations = [
            translate(session=session, target_language=chat.language, topic=topic) for topic in summary.topics
        ]
        session.add_all(translations)
        msg = "\n".join(f"- {translation.target_text}" for translation in translations)
    else:
        msg = "\n".join(f"- {topic.text}" for topic in summary.topics)

    await update.message.reply_text(msg)


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
        new_email = tg_user.user.email != email_address
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
        elif not tg_user.user.token.active:
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
        elif tg_user.user.token.active and not new_email:
            await update.message.reply_markdown_v2(r"Your email is already activated\. Everything is fine\. 😊")
            return

    user = User(telegram_user=tg_user, email=email_address, token=Token())
    session.add(user)
    session.commit()

    await send_token_email(update, context, session=session)


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
        "token": tg_user.user.token.value,
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
    token = Token(user=tg_user.user)
    session.add(token)
    session.commit()
    await send_token_email(update, context)
    return


async def dispatch_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Parses the CallbackQuery and updates the message text."""
    query = update.callback_query
    if query is None or query.data is None:
        raise ValueError("The update must contain a callback query and data.")

    # CallbackQueries need to be answered, even if no notification to the user is needed
    # Some clients may have trouble otherwise. See https://core.telegram.org/bots/api#callbackquery
    await query.answer()

    callback_data = cast(dict[str, Any], query.data)
    fnc_key = callback_data["fnc"]

    callback_fnc_mapping = {"resend_email": send_token_email, "edit_email": edit_email}
    fnc: Callable = callback_fnc_mapping[fnc_key]
    args: list = callback_data.get("args", [])
    kwargs: dict = callback_data.get("kwargs", {})
    await fnc(update, context, *args, **kwargs)


async def handle_invalid_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.callback_query or not update.effective_message:
        raise ValueError("update needs callbackquery and effective message")
    """Informs the user that the button is no longer available."""
    await update.callback_query.answer()
    await update.effective_message.edit_reply_markup(reply_markup=None)
    await update.effective_message.reply_markdown_v2(
        "Sorry, I could not process this button click\. Button seems to be not available anymore 😕"
    )


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

    stmt = select(Token).where(Token.value == context.args[0])
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
        session.add(token)
        # TODO: add 10 day free premium trial
        await update.message.reply_markdown_v2(r"""✅ Your account has been activated\! 🚀""")
        return


@ensure_chat
async def catch_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        raise ValueError("The update must contain a message.")
    else:
        await update.message.reply_text(MSG)


def main() -> None:
    check_database_languages()

    """Start the bot."""
    # Create the Application and pass it your bot's token.
    if telegram_bot_token := os.getenv("TELEGRAM_BOT_TOKEN"):
        application = Application.builder().token(telegram_bot_token).arbitrary_callback_data(True).build()
    else:
        raise ValueError("TELEGRAM_BOT_TOKEN environment variable is not set.")

    # on different commands - answer in Telegram
    commands_to_set = []
    for func, command, description in (
        (start, "start", "Say hi to the bot"),
        (
            set_lang,
            "lang",
            "Set default language for summaries (default is English)",
        ),  # TODO new logic: quickdial, default
        (start, "help", "Show help message"),  # TODO write a help message
        (register, "register", "Register a new email address"),
        (activate, "activate", "Activate a token"),
    ):
        application.add_handler(CommandHandler(command, func))
        bot_command: BotCommand = BotCommand(command, description)
        commands_to_set.append(bot_command)

    # on non command i.e message - echo the message on Telegram
    application.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, transcribe_and_summarize))
    application.add_handler(MessageHandler(filters.ALL & ~filters.VOICE & ~filters.AUDIO & ~filters.COMMAND, catch_all))
    application.add_handler(CallbackQueryHandler(handle_invalid_button, pattern=InvalidCallbackData))
    application.add_handler(CallbackQueryHandler(dispatch_callback))

    # all are coroutines (async/await)
    post_init_fncs = [["delete_my_commands"], ["set_my_commands", commands_to_set]]

    async def post_init(self):
        for fnc, *args in post_init_fncs:
            fnc = getattr(self.bot, fnc)
            await fnc(*args)

    application.post_init = post_init
    # Run the bot until the user presses Ctrl-C
    if webhook_url := os.getenv("TELEGRAM_WEBHOOK_URL"):
        parsed_url = urlparse(webhook_url)
        application.run_webhook(
            listen="0.0.0.0",
            port=8443,
            url_path=parsed_url.path[1:],  # omit the '/' at the beginning of the path
            secret_token=os.getenv("TELEGRAM_WEBHOOK_SECRET_TOKEN", ""),
            webhook_url=webhook_url,
        )
    else:
        post_init_fncs.append(["delete_webhook"])
        application.run_polling()


if __name__ == "__main__":
    main()
