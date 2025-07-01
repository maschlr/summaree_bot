import asyncio
import binascii
import json
import os

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


from typing import Optional, Sequence, Union, cast

from sqlalchemy import select
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ..logging import getLogger
from ..models import Language, TelegramChat, Transcript, Translation
from ..models.session import DbSessionContext
from ..templates import get_template
from ..utils import url
from . import BotMessage
from .audio import _get_audio_summary_message
from .constants import (
    FREE_LANGUAGE_IETF_TAGS,
    LANG_TO_RECEIVED_AUDIO_MESSAGE,
    UI_TRANSLATION_IETF_TAGS,
)
from .db import ensure_chat, session_context
from .helpers import escape_markdown
from .premium import get_sale_text, get_subscription_keyboard, referral

# Enable logging
_logger = getLogger(__name__)

_t = Translation.get

__all__ = [
    "start",
    "set_lang",
    "catch_all",
]


@session_context
@ensure_chat
def _set_lang(update: Update, context: DbSessionContext) -> BotMessage:
    """Set the target language when /lang {language_code} is issued."""
    if update.effective_chat is None:
        raise ValueError("The update must contain a message.")

    session = context.db_session
    if session is None:
        raise ValueError("The context must contain a database session.")
    chat = session.get(TelegramChat, update.effective_chat.id)
    if chat is None:
        raise ValueError(f"Could not find chat with id {update.effective_chat.id}")

    stmt = select(Language)
    languages = session.scalars(stmt).all()
    if not languages:
        raise ValueError("No languages found in database.")

    ietf_tag = update.effective_user.language_code

    def get_lang_msg(
        prefix: str,
        target_languages: Sequence[Language] = languages,
        suffix: str = "",
        translations: Optional[dict[str, str]] = None,
    ) -> str:
        template = get_template("lang", update)
        return template.render(prefix=prefix, languages=target_languages, suffix=suffix, translations=translations)

    example_suffix_lines = [
        "Examples:",
        "🇺🇸 For English type: `/lang en`",
        "🇪🇸 Para Español escribe `/lang es`",
        "🇩🇪 Für Deutch, schreibe `/lang es`",
        "🇷🇺 Для Русского напишите `/lang ru`\n",
        "Or choose a button below:",
    ]
    if ietf_tag in UI_TRANSLATION_IETF_TAGS:
        translations = _t(session, set([example_suffix_lines[0], example_suffix_lines[-1]]), ietf_tag)
        first_line, last_line = [translations[line] for line in [example_suffix_lines[0], example_suffix_lines[-1]]]
        example_suffix = "\n".join([first_line, *example_suffix_lines[1:-1], last_line])
    else:
        translations = {}
        example_suffix = "\n".join(example_suffix_lines)

    if not context.args:
        # Give the user an inline keyboard to choose from
        # make the inline keyboard pageable
        # first page has only "Next >>" button to go to the next page, last page only "<< Previous" button
        # Give the user 4 options, not the one they already have
        if ietf_tag in FREE_LANGUAGE_IETF_TAGS:
            to_translate = [chat.language.name, *[lang.name for lang in languages]]
            translations.update(_t(session, set(to_translate), ietf_tag))
            lang_txt = escape_markdown(
                f"{chat.language.flag_emoji} {chat.language.ietf_tag} [{translations[chat.language.name]}]"
            )
        else:
            lang_txt = escape_markdown(f"{chat.language.flag_emoji} {chat.language.ietf_tag} [{chat.language.name}]")

        prefix_template = get_template("lang_prefix", update)
        prefix = prefix_template.render(lang_txt=lang_txt)

        reply_markup = _get_lang_inline_keyboard(update, context)
        kwargs = dict(prefix=prefix, target_languages=languages, suffix=example_suffix)
        if ietf_tag in UI_TRANSLATION_IETF_TAGS:
            kwargs["translations"] = translations
        text = get_lang_msg(**kwargs)
        return BotMessage(
            chat_id=chat.id,
            text=text,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=reply_markup,
        )

    target_language_ietf_tag = context.args[0].lower()
    if target_language_ietf_tag not in {lang.ietf_tag for lang in languages}:
        lang_to_prefix = {
            "en": "Unknown language\.\n Available languages are:\n",
            "ru": "Неизвестный язык\.\n Доступные языки:\n",
            "es": "Idioma desconocido\.\n Idiomas disponibles:\n",
            "de": "Unbekannte Sprache\.\n Verfügbare Sprachen:\n",
        }
        ietf_tag = update.effective_user.language_code
        prefix = lang_to_prefix.get(ietf_tag, lang_to_prefix["en"])
        return BotMessage(
            chat_id=chat.id,
            text=get_lang_msg(prefix, languages, example_suffix),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_get_lang_inline_keyboard(update, context),
        )

    if not chat.is_premium_active and context.args and target_language_ietf_tag not in FREE_LANGUAGE_IETF_TAGS:
        lang_to_prefix_template = {
            "en": (
                "Setting an output different than English, Spanish, German or Russian is a premium feature\. "
                "With premium active, you will be able to choose from {n_languages} different languages:\n"
            ),
            "ru": (
                "Установка языка вывода, отличного от английского, испанского, "
                "немецкого или русского, является премиальной функцией\. "
                "С премиумом вы сможете выбрать из {n_languages} разных языков:\n"
            ),
            "es": (
                "Establecer un idioma de salida diferente a inglés, español, alemán o ruso es una función premium\. "
                "Con premium activo, podrás elegir entre {n_languages} idiomas diferentes:\n"
            ),
            "de": (
                "Festlegen einer Ausgabesprache, die nicht englisch, spanisch, deutsch"
                " oder russisch ist, ist eine Premium-Funktion\. "
                "Mit Premium kannst du aus {n_languages} verschiedenen Sprachen wählen:\n"
            ),
        }
        prefix_template = lang_to_prefix_template.get(ietf_tag, lang_to_prefix_template["en"])
        prefix = prefix_template.format(n_languages=len(languages))
        reply_markup, periods_to_products = get_subscription_keyboard(update, context, return_products=True)

        suffix = get_sale_text(periods_to_products, update)

        kwargs = dict(prefix=prefix, target_languages=languages, suffix=suffix)
        if ietf_tag in UI_TRANSLATION_IETF_TAGS:
            kwargs["translations"] = translations

        return BotMessage(
            chat_id=chat.id,
            text=get_lang_msg(**kwargs),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=reply_markup,
        )

    [target_language] = [lang for lang in languages if lang.ietf_tag == target_language_ietf_tag]
    if ietf_tag in UI_TRANSLATION_IETF_TAGS:
        translations.update(_t(session, {target_language.name}, ietf_tag))
        lang_txt = f"{target_language.flag_emoji} {target_language_ietf_tag} [{translations[target_language.name]}]"
    else:
        lang_txt = f"{target_language.flag_emoji} {target_language_ietf_tag} [{target_language.name}]"

    if chat.language != target_language:
        chat.language = target_language
        lang_txt_templates = {
            "en": "Language successfully set to: {lang_txt}",
            "ru": "Язык успешно установлен: {lang_txt}",
            "es": "Idioma establecido con éxito: {lang_txt}",
            "de": "Sprache erfolgreich auf {lang_txt} gesetzt",
        }

        template = lang_txt_templates.get(ietf_tag, lang_txt_templates["en"])
        text = template.format(lang_txt=lang_txt)
        return BotMessage(
            chat_id=chat.id,
            text=text,
        )
    else:
        other_available_languages = [lang for lang in languages if lang.ietf_tag != target_language_ietf_tag]
        lang_txt_templates = {
            "en": "This language is already configured as the target language: {lang_txt}\n",
            "ru": "Этот язык уже настроен как целевой язык: {lang_txt}\n",
            "es": "Este idioma ya está configurado como el idioma de destino: {lang_txt}\n",
            "de": "Diese Sprache ist bereits als Zielsprache konfiguriert: {lang_txt}\n",
        }

        template = lang_txt_templates.get(ietf_tag, lang_txt_templates["en"])
        prefix = template.format(lang_txt=escape_markdown(lang_txt))

        return BotMessage(
            chat_id=chat.id,
            text=get_lang_msg(prefix, other_available_languages, example_suffix),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_get_lang_inline_keyboard(update, context),
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

    ietf_tag = update.effective_user.language_code
    if ietf_tag in UI_TRANSLATION_IETF_TAGS:
        translations = _t(session, set(lang.name for lang in sorted_languages), ietf_tag)
        language_buttons = [
            InlineKeyboardButton(
                f"{lang.flag_emoji} {translations[lang.name]}",
                callback_data=dict(**callback_data, kwargs={"ietf_tag": lang.ietf_tag}),
            )
            for lang in sorted_languages
        ]
    else:
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

    lang_to_previous_next_button_texts = {
        "en": ["<< Previous", "Next >>"],
        "ru": ["<< Предыдущая", "Следующая >>"],
        "es": ["<< Anterior", "Siguiente >>"],
        "de": ["<< Vorherige", "Nächste >>"],
    }
    previous_next_button_texts = lang_to_previous_next_button_texts.get(
        ietf_tag, lang_to_previous_next_button_texts["en"]
    )

    # indexing of pages starts with 1
    if page == 1:
        buttons_on_page = language_buttons[: items_per_page - 1]
        # only next button at last position of last row
        buttons_on_page.append(
            InlineKeyboardButton(previous_next_button_texts[1], callback_data=dict(**callback_data, kwargs={"page": 2}))
        )
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
            0,
            InlineKeyboardButton(
                previous_next_button_texts[0],
                callback_data=dict(**callback_data, kwargs={"page": page - 1}),
            ),
        )
    else:
        # middle page
        _start = items_per_page - 1 + (page - 2) * (items_per_page - 2)
        buttons_on_page = language_buttons[_start : _start + items_per_page - 2]
        buttons_on_page.insert(
            0,
            InlineKeyboardButton(
                previous_next_button_texts[0],
                callback_data=dict(**callback_data, kwargs={"page": page - 1}),
            ),
        )
        buttons_on_page.append(
            InlineKeyboardButton(
                previous_next_button_texts[1],
                callback_data=dict(**callback_data, kwargs={"page": page + 1}),
            )
        )

    keyboard = InlineKeyboardMarkup(list(batched(buttons_on_page, columns)))
    return keyboard


async def set_lang_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, ietf_tag=None, page=None) -> None:
    """Inline keyboard callback"""
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
def _start(
    update: Update, context: DbSessionContext, commands: Sequence[BotCommand]
) -> Union[BotMessage, Sequence[BotMessage]]:
    if update.message is None or update.effective_user is None:
        raise ValueError("The update must contain a message and a user.")

    fnc_mapping = {
        "ref": referral,
    }
    if context is not None and context.args is not None and len(context.args):
        [b64_data] = context.args
        callback_data = cast(Sequence, url.decode(b64_data))
        fnc_key, *args = callback_data
        fnc = fnc_mapping[fnc_key]
        return fnc(update, context, *args)

    bot_msg = _help_handler(update, context, commands)
    return bot_msg


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start the bot."""
    if update.message is None:
        raise ValueError("The update must contain a message.")

    try:
        commands = await context.bot.get_my_commands()
        msg_or_more = _start(update, context, commands)
        if isinstance(msg_or_more, BotMessage):
            msgs = [msg_or_more]
        else:
            msgs = msg_or_more
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

    async with asyncio.TaskGroup() as tg:
        for msg in msgs:
            tg.create_task(msg.send(context.bot))


async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show help message."""
    commands: Sequence[BotCommand] = await context.bot.get_my_commands()
    bot_msg = _help_handler(update, context, commands)
    await bot_msg.send(context.bot)


@session_context
def _help_handler(update: Update, context: DbSessionContext, commands: Sequence[BotCommand]) -> BotMessage:
    lang_to_button_text = {"ru": "🦾 Покажи мне демо!", "es": "🦾 Demo, por favor!", "de": "🦾 Demo, bitte!"}

    template = get_template("help", update)
    ietf_tag = update.effective_user.language_code
    if ietf_tag in {"ru", "es", "de"}:
        descriptions = [command.description for command in commands]
        translations = _t(context.db_session, descriptions, ietf_tag)
        command_to_translated_description = {command.command: translations[command.description] for command in commands}
        text = template.render(user=update.effective_user, commands=command_to_translated_description)
    else:
        text = template.render(user=update.effective_user, commands=commands)

    # default to en
    button_text = lang_to_button_text.get(ietf_tag, "🦾 Show me a demo!")
    keyboard_button = [InlineKeyboardButton(button_text, callback_data=dict(fnc="demo"))]
    reply_markup = InlineKeyboardMarkup([keyboard_button])

    bot_msg = BotMessage(
        chat_id=update.effective_message.chat_id,
        text=text,
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML,
    )
    return bot_msg


async def support(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang_to_text = {
        "en": "Support is available at [summar\.ee bot support channel](https://t.me/+pfu9RGUlNt05MTZh)",
        "ru": "Поддержка доступна в [супер чате summar\.ee](https://t.me/+pfu9RGUlNt05MTZh)",
        "es": "Soporte disponible en [canal de soporte de summar\.ee](https://t.me/+pfu9RGUlNt05MTZh)",
        "de": "Support verfügbar im [summar\.ee Bot Support Channel](https://t.me/+pfu9RGUlNt05MTZh)",
    }
    text = lang_to_text.get(update.effective_user.language_code, lang_to_text["en"])

    msg = BotMessage(
        text=text,
        chat_id=update.effective_chat.id,
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    await msg.send(context.bot)


async def paysupport(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await support(update, context)


async def terms(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/terms handler"""
    template = get_template("terms", update)
    text = template.render()

    msg = BotMessage(
        text=text,
        chat_id=update.effective_chat.id,
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    await msg.send(context.bot)


@session_context
@ensure_chat
def _catch_all(update: Update, context: DbSessionContext) -> Optional[BotMessage]:
    return None


async def catch_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if bot_msg := _catch_all(update, context):
        await bot_msg.send(context.bot)


async def demo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # post the audio file
    bot = context.bot
    await bot.send_audio(chat_id=update.effective_chat.id, audio=os.getenv("DEMO_FILE_ID"))

    text = LANG_TO_RECEIVED_AUDIO_MESSAGE.get(update.effective_user.language_code, LANG_TO_RECEIVED_AUDIO_MESSAGE["en"])
    reply = await update.effective_message.reply_text(text)
    # wait one second
    await asyncio.sleep(1)

    # get the transcript, delete the reply message
    msg = _demo(update, context)
    async with asyncio.TaskGroup() as tg:
        tg.create_task(reply.delete())
        tg.create_task(msg.send(bot))


@session_context
@ensure_chat
def _demo(update: Update, context: DbSessionContext) -> BotMessage:
    session = context.db_session

    stmt = select(Transcript).where(
        Transcript.sha256_hash == "f5d703775735e608396db4a8bf088a4d581fcc06fda2ae38c7f0e793b9f1b6bd"
    )
    transcript = session.execute(stmt).scalar_one()
    msg = _get_audio_summary_message(update, context, transcript.summary)

    return msg
