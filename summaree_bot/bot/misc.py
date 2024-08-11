import asyncio
from typing import Any, Callable, cast

from telegram import Update
from telegram.ext import ContextTypes

from .audio import elaborate, translate_transcript
from .premium import payment_callback
from .user import set_lang_callback

__all__ = ["remove_inline_keyboard", "dispatch_callback"]


async def remove_inline_keyboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback Handler to remove inline keyboard"""
    if not update.callback_query or not update.effective_message:
        raise ValueError("update needs callbackquery and effective message")
    await update.callback_query.answer()
    await update.effective_message.edit_reply_markup(reply_markup=None)


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

    callback_fnc_mapping: dict[str, Callable] = {
        "remove_inline_keyboard": remove_inline_keyboard,
        "buy_or_extend_subscription": payment_callback,
        "set_lang": set_lang_callback,
        "elaborate": elaborate,
        "translate_transcript": translate_transcript,
    }
    fnc: Callable = callback_fnc_mapping[fnc_key]
    args: list = callback_data.get("args", [])
    kwargs: dict = callback_data.get("kwargs", {})
    await fnc(update, context, *args, **kwargs)


async def process_message_queue(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Process the message queue"""
    async with asyncio.TaskGroup() as tg:
        while context.bot_data["message_queue"]:
            msg = context.bot_data["message_queue"].pop()
            tg.create_task(msg.send(context.bot))
