import os
from urllib.parse import urlparse

from telegram import BotCommand
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    InvalidCallbackData,
    MessageHandler,
    filters,
)

from summaree_bot.bot.audio import transcribe_and_summarize
from summaree_bot.bot.error import (
    bad_command_handler,
    error_handler,
    invalid_button_handler,
)
from summaree_bot.bot.user import (
    activate,
    catch_all,
    dispatch_callback,
    register,
    set_lang,
    start,
)
from summaree_bot.integrations import check_database_languages
from summaree_bot.logging import getLogger

# Enable logging
_logger = getLogger(__name__)


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

    application.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, transcribe_and_summarize))
    application.add_handler(MessageHandler(filters.ALL & ~filters.VOICE & ~filters.AUDIO & ~filters.COMMAND, catch_all))
    application.add_handler(CallbackQueryHandler(invalid_button_handler, pattern=InvalidCallbackData))
    application.add_handler(CallbackQueryHandler(dispatch_callback))
    application.add_handler(CommandHandler("bad_command", bad_command_handler))
    # ...and the error handler
    application.add_error_handler(error_handler)

    # all are coroutines (async/await)
    post_init_fncs = [["delete_my_commands"], ["set_my_commands", commands_to_set]]

    async def post_init(self):
        for fnc, *args in post_init_fncs:
            fnc = getattr(self.bot, fnc)
            await fnc(*args)

    application.post_init = post_init
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
        )
    else:
        post_init_fncs.append(["delete_webhook"])
        application.run_polling()


if __name__ == "__main__":
    main()
