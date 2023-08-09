from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Iterator, Optional, Sequence, Union

from telegram import (
    ForceReply,
    InlineKeyboardMarkup,
    LabeledPrice,
    MessageEntity,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.constants import ParseMode
from telegram.ext import ExtBot


@dataclass
class BotResponse(Mapping, ABC):
    chat_id: Union[int, str]

    def __getitem__(self, __key: Any) -> Any:
        return asdict(self)[__key]

    def __iter__(self) -> Iterator[Any]:
        yield from asdict(self)

    def __len__(self) -> int:
        return len(asdict(self))

    @abstractmethod
    async def send(self, bot: ExtBot) -> None:
        raise NotImplementedError()


@dataclass
class BotMessage(BotResponse):
    text: str
    parse_mode: Optional[ParseMode] = None
    entities: Optional[Sequence[MessageEntity]] = None
    disable_web_page_preview: Optional[bool] = None
    disable_notification: Optional[bool] = None
    protect_content: Optional[bool] = None
    reply_to_message_id: Optional[int] = None
    allow_sending_without_reply: Optional[bool] = None
    reply_markup: Optional[Union[InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove, ForceReply]] = None
    message_thread_id: Optional[int] = None
    read_timeout: Union[float, None] = None
    write_timeout: Union[float, None] = None
    connect_timeout: Union[float, None] = None
    pool_timeout: Union[float, None] = None
    api_kwargs: Optional[dict] = None

    async def send(self, bot: ExtBot) -> None:
        await bot.send_message(**self)


# https://docs.python-telegram-bot.org/en/stable/telegram.bot.html#telegram.Bot.send_invoice
@dataclass
class BotInvoice(BotResponse):
    title: str
    description: str
    payload: str
    provider_token: str
    currency: str
    prices: Sequence[LabeledPrice]
    start_parameter: Optional[str] = None
    photo_url: Optional[str] = None
    photo_size: Optional[int] = None
    photo_width: Optional[int] = None
    photo_height: Optional[int] = None
    need_name: Optional[bool] = None
    need_phone_number: Optional[bool] = None
    need_email: Optional[bool] = None
    need_shipping_address: Optional[bool] = None
    is_flexible: Optional[bool] = None
    disable_notification: Optional[bool] = None
    reply_to_message_id: Optional[int] = None
    reply_markup: Optional[InlineKeyboardMarkup] = None
    provider_data: Optional[Union[str, object]] = None
    send_phone_number_to_provider: Optional[bool] = None
    send_email_to_provider: Optional[bool] = None
    allow_sending_without_reply: Optional[bool] = None
    max_tip_amount: Optional[int] = None
    suggested_tip_amounts: Optional[Sequence[int]] = None
    protect_content: Optional[bool] = None
    message_thread_id: Optional[int] = None
    read_timeout: Optional[float] = None
    write_timeout: Optional[float] = None
    connect_timeout: Optional[float] = None
    pool_timeout: Optional[float] = None
    api_kwargs: Optional[dict] = None

    async def send(self, bot: ExtBot) -> None:
        await bot.send_invoice(**self)


def escape_markdown(text: str) -> str:
    """Helper function to escape telegram markup symbols"""
    escape_chars = r"\*_\[]().!#+{}~>-"
    return "".join(rf"\{c}" if c in escape_chars else c for c in text)
