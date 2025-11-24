import json
import traceback
from typing import Iterator, Union

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from ..logging import getLogger
from . import AdminChannelMessage
from .helpers import wrap_in_pre

# Enable logging
_logger = getLogger(__name__)

__all__ = ["error_handler", "invalid_button_handler", "bad_command_handler"]


async def invalid_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Informs the user that the button is no longer available."""
    if not update.callback_query or not update.effective_message:
        raise ValueError("update needs callbackquery and effective message")
    await update.callback_query.answer()
    await update.effective_message.edit_reply_markup(reply_markup=None)
    await update.effective_message.reply_markdown_v2(
        r"Sorry, I could not process this button click\. Button seems to be not available anymore 😕"
    )


def _error_handler(update: Union[Update, object], context: ContextTypes.DEFAULT_TYPE) -> Iterator[AdminChannelMessage]:
    """Log the error and send a telegram message to notify the developer."""
    # Log the error before we do anything else, so we can see it even if something breaks.
    if isinstance(context.error, BadRequest) and "Not enough rights" in context.error.message:
        return
    _logger.error("Exception while handling an update:", exc_info=context.error)

    # traceback.format_exception returns the usual python message about an exception, but as a
    # list of strings rather than a single string, so we have to join them together.
    traceback_msg = context.error.__traceback__ if context.error else None
    tb_list = traceback.format_exception(None, context.error, traceback_msg)
    tb_string = "".join(tb_list)

    # Build the message with some markup and additional information about what happened.
    message_generic = "An exception was raised while handling an update"
    message_traceback = wrap_in_pre(tb_string)
    update_str = update.to_dict() if isinstance(update, Update) else str(update)
    message_update = wrap_in_pre(
        f"update = {json.dumps(update_str, indent=2, ensure_ascii=False)}\n"
        f"context.chat_data = {str(context.chat_data)}\n\n"
        f"context.user_data = {str(context.user_data)}\n"
    )

    for msg in (message_generic, message_traceback, message_update):
        yield AdminChannelMessage(text=msg[:4096], parse_mode=ParseMode.HTML, pool_timeout=10)


async def error_handler(update: Union[Update, object], context: ContextTypes.DEFAULT_TYPE) -> None:
    for bot_msg in _error_handler(update, context):
        await bot_msg.send(context.bot)


async def bad_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Raise an error to trigger the error handler."""
    await context.bot.wrong_method_name()  # type: ignore[attr-defined]
