from sqlalchemy import select
from telegram import Update

from ..models import TelegramUser, User
from ..models.session import DbSessionContext
from .helpers import add_session, ensure_chat


@add_session
@ensure_chat
async def referral(update: Update, context: DbSessionContext) -> None:
    if update is None or update.message is None:
        raise ValueError("update is None")
    # case 1: telegram_user has no user -> /register first
    session = context.db_session
    tg_user = session.get(TelegramUser, update.effective_user.id)
    if tg_user is None or tg_user.user is None:
        await update.message.reply_markdown_v2(
            "✋💸 In order to use referrals, you need to `/register` your email first\."
        )
        return
    elif not tg_user.user.email_token.active:
        await update.message.reply_markdown_v2(
            "✋💸 Your email is not verified\. Please check your inbox and click the link in the email\. "
            "Use `/register` to re-send email or change email address\."
        )
    elif not context.args:
        # case 2: no context.args -> list token and referred users
        n_referrals = len(tg_user.user.referrals)
        await update.message.reply_markdown_v2(
            f"👥 Your referral token is `{tg_user.user.referral_token}`\.\n\n"
            f"💫 You have referred {n_referrals} users\. "
            f"In total, you have received {n_referrals*7} days of free premium\! 💸"
        )
    # case 3: context.args[0] is a token -> add referral
    else:
        stmt = select(User).where(User.referral_token == context.args[0])
        referrer = session.execute(stmt).scalar_one_or_none()
        if referrer is None:
            await update.message.reply_markdown_v2("🤷‍♀️🤷‍♂️ This referral token is not valid\.")
        else:
            tg_user.user.referrer = referrer
            await update.message.reply_markdown_v2(
                "👍 You have successfully used this referral token\. "
                "You and the referrer will both receive one week of premium for free! 💫💸"
            )


def premium(update: Update, context: DbSessionContext) -> None:
    # TODO
    # case 1: telegram_user has no user -> /register first
    # case 2: user has active subscription
    #   -> show subscription info
    #   -> ask user if subscription should be extended
    # case 3: user has no active subscription
    #  -> ask user if subscription should be bought
    pass
