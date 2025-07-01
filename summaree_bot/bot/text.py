from typing import Any, Coroutine

from telegram import Update
from telegram.ext import ContextTypes

from ..models import Summary
from ..models.session import DbSessionContext, session_context
from . import BotMessage


async def get_text_summary_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> Coroutine[Any, Any, BotMessage]:
    pass


@session_context
def _get_text_summary_message(update: Update, context: DbSessionContext, summary: Summary) -> BotMessage:
    pass
