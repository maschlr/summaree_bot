import json
import os
import traceback
from typing import Iterator

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ..logging import getLogger
from . import BotMessage
from .helpers import escape_markdown

# Enable logging
_logger = getLogger(__name__)

__all__ = ["error_handler", "invalid_button_handler", "bad_command_handler"]


async def invalid_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.callback_query or not update.effective_message:
        raise ValueError("update needs callbackquery and effective message")
    """Informs the user that the button is no longer available."""
    await update.callback_query.answer()
    await update.effective_message.edit_reply_markup(reply_markup=None)
    await update.effective_message.reply_markdown_v2(
        "Sorry, I could not process this button click\. Button seems to be not available anymore 😕"
    )


def _error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Iterator[BotMessage]:
    """Log the error and send a telegram message to notify the developer."""
    # Log the error before we do anything else, so we can see it even if something breaks.
    _logger.error("Exception while handling an update:", exc_info=context.error)

    # traceback.format_exception returns the usual python message about an exception, but as a
    # list of strings rather than a single string, so we have to join them together.
    traceback_msg = context.error.__traceback__ if context.error else None
    tb_list = traceback.format_exception(None, context.error, traceback_msg)
    tb_string = "".join(tb_list)

    # Build the message with some markup and additional information about what happened.
    message_traceback = "An exception was raised while handling an update\n" f"```\n{tb_string}\n```"
    update_str = update.to_dict() if isinstance(update, Update) else str(update)
    message_update = (
        "```\n"
        f"update = {json.dumps(update_str, indent=2, ensure_ascii=False)}\n"
        f"context.chat_data = {str(context.chat_data)}\n\n"
        f"context.user_data = {str(context.user_data)}\n"
        "```"
    )

    admin_chat_id = os.getenv("ADMIN_CHAT_ID")
    if admin_chat_id is None:
        raise ValueError("ADMIN_CHAT_ID environment variable not set")

    for msg in (message_traceback, message_update):
        escaped_msg = escape_markdown(msg)
        yield BotMessage(chat_id=admin_chat_id, text=escaped_msg[:4096], parse_mode=ParseMode.MARKDOWN, pool_timeout=10)


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    for bot_msg in _error_handler(update, context):
        await bot_msg.send(context.bot)


async def bad_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Raise an error to trigger the error handler."""
    await context.bot.wrong_method_name()  # type: ignore[attr-defined]
