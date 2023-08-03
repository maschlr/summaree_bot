import html
import json
import os
import traceback

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ..logging import getLogger

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


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error and send a telegram message to notify the developer."""
    # Log the error before we do anything else, so we can see it even if something breaks.
    _logger.error("Exception while handling an update:", exc_info=context.error)

    # traceback.format_exception returns the usual python message about an exception, but as a
    # list of strings rather than a single string, so we have to join them together.
    traceback_msg = context.error.__traceback__ if context.error else None
    tb_list = traceback.format_exception(None, context.error, traceback_msg)
    tb_string = "".join(tb_list)

    # Build the message with some markup and additional information about what happened.
    update_str = update.to_dict() if isinstance(update, Update) else str(update)

    message_basic = (
        f"An exception was raised while handling an update\n"
        f"<pre>update = {html.escape(json.dumps(update_str, indent=2, ensure_ascii=False))}"
        "</pre>\n\n"
        f"<pre>context.chat_data = {html.escape(str(context.chat_data))}</pre>\n\n"
        f"<pre>context.user_data = {html.escape(str(context.user_data))}</pre>\n\n"
    )
    message_traceback = f"<pre>{html.escape(tb_string)}</pre>"

    if len(message_basic + message_traceback) >= 4096:
        message = message_basic
    else:
        message = message_basic + message_traceback

    # Finally, send the message
    admin_chat_id = os.getenv("ADMIN_CHAT_ID", 0)
    await context.bot.send_message(chat_id=admin_chat_id, text=message, parse_mode=ParseMode.HTML)


async def bad_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Raise an error to trigger the error handler."""
    await context.bot.wrong_method_name()  # type: ignore[attr-defined]
