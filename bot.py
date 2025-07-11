import os
from collections import deque
from urllib.parse import urlparse

from telegram import BotCommand
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    InvalidCallbackData,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

from summaree_bot.bot.admin import command_to_handler
from summaree_bot.bot.common import process_transcription_request_message
from summaree_bot.bot.db import chat_migration
from summaree_bot.bot.error import (
    bad_command_handler,
    error_handler,
    invalid_button_handler,
)
from summaree_bot.bot.misc import dispatch_callback, process_message_queue
from summaree_bot.bot.premium import (
    precheckout_callback,
    premium_handler,
    referral_handler,
    successful_payment_callback,
)
from summaree_bot.bot.user import (
    catch_all,
    exclude_lang,
    help_handler,
    paysupport,
    set_lang,
    start,
    support,
    terms,
)
from summaree_bot.integrations import check_database_languages
from summaree_bot.logging import getLogger
from summaree_bot.models import Subscription

# Enable logging
_logger = getLogger(__name__)


def main() -> None:
    """Start the bot."""
    check_database_languages()
    # Create the Application and pass it your bot's token.
    if telegram_bot_token := os.getenv("TELEGRAM_BOT_TOKEN"):
        application = (
            Application.builder()
            .token(telegram_bot_token)
            .arbitrary_callback_data(True)
            .pool_timeout(5)
            .concurrent_updates(True)
            .build()
        )
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
        ),
        (help_handler, "help", "Show help message"),
        (support, "support", "Get support"),
        (premium_handler, "premium", "Manage premium features"),
        (paysupport, "paysupport", "Get support for payments"),
        (terms, "terms", "Show terms of service"),
    ):
        application.add_handler(CommandHandler(command, func))
        bot_command: BotCommand = BotCommand(command, description)
        commands_to_set.append(bot_command)

    admin_chat_id = os.getenv("ADMIN_CHAT_ID")
    if admin_chat_id is None:
        raise ValueError("ADMIN_CHAT_ID environment variable not set")

    # Admin channel commands
    for command, (handler, _description) in command_to_handler.items():
        application.add_handler(CommandHandler(command, handler, filters.Chat(int(admin_chat_id))))

    application.add_handler(CommandHandler("referral", referral_handler))
    application.add_handler(
        MessageHandler(
            filters.UpdateType.MESSAGE & (filters.VOICE | filters.AUDIO | filters.Document.Category("audio/")),
            process_transcription_request_message,
        )
    )
    application.add_handler(
        MessageHandler(
            filters.UpdateType.MESSAGE & (filters.VIDEO | filters.VIDEO_NOTE | filters.Document.Category("video/")),
            process_transcription_request_message,
        )
    )
    application.add_handler(CallbackQueryHandler(invalid_button_handler, pattern=InvalidCallbackData))
    application.add_handler(CallbackQueryHandler(dispatch_callback))
    application.add_handler(CommandHandler("bad_command", bad_command_handler))

    # Add command handler to start the payment invoice
    application.add_handler(CommandHandler("premium", premium_handler))
    # We don't add this to the menu since this is a hidden feature
    application.add_handler(CommandHandler("exclude", exclude_lang))

    # Pre-checkout handler to final check
    application.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    # Success! Notify your user!
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))

    application.add_handler(
        MessageHandler(
            filters.ALL & ~filters.VOICE & ~filters.AUDIO & ~filters.COMMAND & ~filters.SUCCESSFUL_PAYMENT, catch_all
        )
    )
    # chat migrating to SuperChat
    application.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, chat_migration))
    # ...and the error handler
    application.add_error_handler(error_handler)

    # all are coroutines (async/await)
    post_init_fncs = [["delete_my_commands"], ["set_my_commands", commands_to_set]]

    async def post_init(self):
        os.environ["BOT_LINK"] = self.bot.link
        for fnc, *args in post_init_fncs:
            fnc = getattr(self.bot, fnc)
            await fnc(*args)

    application.post_init = post_init
    application.job_queue.run_repeating(Subscription.update_subscription_status, interval=60 * 30, first=10)
    application.bot_data["message_queue"] = deque()
    application.job_queue.run_repeating(process_message_queue, interval=60, first=0)

    _logger.info("Starting Summar.ee Bot")
    # Run the bot until the user presses Ctrl-C
    if webhook_url := os.getenv("TELEGRAM_WEBHOOK_URL"):
        parsed_url = urlparse(webhook_url)
        application.run_webhook(
            listen="0.0.0.0",
            port=8443,
            url_path=parsed_url.path[1:],  # omit the '/' at the beginning of the path
            secret_token=os.getenv("TELEGRAM_WEBHOOK_SECRET_TOKEN", ""),
            webhook_url=webhook_url,
            bootstrap_retries=3,
        )
    else:
        post_init_fncs.append(["delete_webhook"])
        application.run_polling()


if __name__ == "__main__":
    main()
