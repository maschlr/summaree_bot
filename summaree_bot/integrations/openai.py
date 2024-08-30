import asyncio
import datetime as dt
import hashlib
import json
import logging
import os
import re
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Generator, Optional, Union, cast

import telegram
from openai import AsyncOpenAI, BadRequestError, OpenAI
from sqlalchemy import func, select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import MessageLimit
from telegram.ext import ContextTypes

from ..bot import BotDocument, BotMessage, ensure_chat
from ..models import Language, Summary, TelegramChat, Topic, Transcript
from ..models.session import DbSessionContext, Session, session_context
from .audio import split_audio, transcode_ffmpeg
from .deepl import translator

_logger = logging.getLogger(__name__)

mimetype_pattern = re.compile(r"(?P<type>\w+)/(?P<subtype>\w+)")
summary_prompt_file_path = Path(__file__).parent / "data" / "summarize.txt"
client = OpenAI()

__all__ = ["_check_existing_transcript", "_extract_file_name", "transcribe_file", "_summarize", "_elaborate"]


@session_context
@ensure_chat
def _check_existing_transcript(
    update: telegram.Update, context: DbSessionContext
) -> tuple[Optional[Transcript], Union[telegram.Voice, telegram.Audio]]:
    if update.message is None or (update.message.voice is None and update.message.audio is None):
        raise ValueError("The update must contain a voice or audio message.")

    session = context.db_session
    if session is None:
        raise ValueError("There should be a session attached to context")
    voice_or_audio = cast(Union[telegram.Voice, telegram.Audio], (update.message.voice or update.message.audio))
    file_unique_id = voice_or_audio.file_unique_id

    stmt = select(Transcript).where(Transcript.file_unique_id == file_unique_id)
    if transcript := session.scalars(stmt).one_or_none():
        _logger.info(f"Using already existing transcript: {transcript} with file_unique_id: {file_unique_id}")
    return transcript, voice_or_audio


def _extract_file_name(voice_or_audio: Union[telegram.Voice, telegram.Audio]) -> Path:
    if hasattr(voice_or_audio, "file_name") and voice_or_audio.file_name is not None:
        return Path(voice_or_audio.file_name)

    # else try to extract the suffix via the mime type or use file_name without suffic
    match = None
    if mime_type := voice_or_audio.mime_type:
        match = mimetype_pattern.match(mime_type)

    if match is None:
        file_name = voice_or_audio.file_unique_id
    else:
        file_name = f"{voice_or_audio.file_unique_id}.{match.group('subtype')}"

    return Path(file_name)


async def transcribe_file(
    update: telegram.Update,
    context: ContextTypes.DEFAULT_TYPE,
    file_path: Path,
    voice_or_audio: Union[telegram.Voice, telegram.Audio],
) -> Transcript:
    with open(file_path, "rb") as fp:
        m = hashlib.sha256()
        while chunk := fp.read(8192):
            m.update(chunk)
        sha256_hash = m.hexdigest()

    with Session.begin() as session:
        stmt = select(Transcript).where(Transcript.sha256_hash == sha256_hash)
        if transcript := session.execute(stmt).scalar_one_or_none():
            _logger.info(f"Using already existing transcript: {transcript} with sha256_hash: {sha256_hash}")
            return transcript

    if (
        update.message is None
        or (update.message.voice is None and update.message.audio is None)
        or update.effective_user is None
    ):
        raise ValueError("The update must contain a voice or audio message (and an effective_user).")
    voice_or_audio = cast(Union[telegram.Voice, telegram.Audio], (update.message.voice or update.message.audio))

    # convert the unsupported file (e.g. .ogg for normal voice) to .mp3
    if file_path.suffix[1:] not in {
        "flac",
        "mp3",
        "mp4",
        "mpeg",
        "mpga",
        "m4a",
        "ogg",
        "wav",
        "webm",
    }:
        supported_file_path = transcode_ffmpeg(file_path)
    else:
        supported_file_path = file_path

    # send the audio file to openai whisper, create a db entry and return it
    whisper_transcription = await get_whisper_transcription(supported_file_path)

    with Session.begin() as session:
        if transcript_language_str := whisper_transcription.language:
            query = select(Language).where(func.regexp_like(Language.name, f"^{transcript_language_str.capitalize()}"))
            transcript_language = session.execute(query).scalar_one_or_none()
        else:
            transcript_language = None

        transcript = Transcript(
            created_at=update.effective_message.date,
            finished_at=dt.datetime.now(dt.UTC),
            file_unique_id=voice_or_audio.file_unique_id,
            file_id=voice_or_audio.file_id,
            sha256_hash=sha256_hash,
            duration=voice_or_audio.duration,
            mime_type=voice_or_audio.mime_type,
            file_size=voice_or_audio.file_size,
            result=whisper_transcription.text,
            input_language=transcript_language,
        )
        session.add(transcript)
        session.flush()
        return transcript


@dataclass
class WhisperTranscription:
    text: str
    language: str


async def get_whisper_transcription(file_path: Path):
    if file_path.stat().st_size > 24 * 1024 * 1024:
        temp_dir = tempfile.TemporaryDirectory()
        file_paths = split_audio(file_path, max_size_mb=24, output_dir=Path(temp_dir.name))
    else:
        temp_dir = None
        file_paths = [file_path]

    tasks = []
    async with asyncio.TaskGroup() as tg:
        for file_path in file_paths:
            task = tg.create_task(get_whisper_transcription_async(file_path))
            tasks.append(task)

    languages = []
    texts = []
    for task in tasks:
        transcription_result = task.result()
        languages.append(transcription_result.model_extra.get("language"))
        texts.append(transcription_result.text)

    language_counter = Counter(languages)
    try:
        most_common_language = language_counter.most_common(1)[0][0]
    except IndexError:
        _logger.warning("Could not determine language of the transcription")
        most_common_language = None

    result = WhisperTranscription(text="\n".join(texts), language=most_common_language)

    if temp_dir is not None:
        temp_dir.cleanup()

    return result


async def get_whisper_transcription_async(file_path: Path):
    aclient = AsyncOpenAI()
    with open(file_path, "rb") as fp:
        try:
            transcription_result = await aclient.audio.transcriptions.create(
                model="whisper-1",
                file=fp,
                response_format="verbose_json",
            )
        except BadRequestError:
            supported_file_path = transcode_ffmpeg(file_path)
            with open(supported_file_path, "rb") as fp:
                transcription_result = await aclient.audio.transcriptions.create(
                    model="whisper-1",
                    file=fp,
                    response_format="verbose_json",
                )
    return transcription_result


@session_context
def _summarize(update: telegram.Update, context: DbSessionContext, transcript: Transcript) -> Summary:
    session = context.db_session
    session.add(transcript)

    # if the transcript is already summarized return it
    if transcript.summary is not None:
        return transcript.summary

    with open(summary_prompt_file_path) as fp:
        system_msg = fp.read()

    user_message = transcript.result
    messages = [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": user_message},
    ]

    created_at = dt.datetime.now(dt.UTC)
    summary_data = get_openai_chatcompletion(messages)

    if transcript.input_language is None:
        language_stmt = select(Language).where(Language.ietf_tag == summary_data["language"])
        if not (language := session.scalars(language_stmt).one_or_none()):
            _logger.warning(f"Could not find language with ietf_tag {summary_data['language']}")
        else:
            transcript.input_language = language

    summary = Summary(
        created_at=created_at,
        finished_at=dt.datetime.now(dt.UTC),
        transcript=transcript,
        topics=[Topic(text=text, order=i) for i, text in enumerate(summary_data["topics"], start=1)],
        tg_user_id=update.effective_user.id,
        tg_chat_id=update.effective_chat.id,
    )
    session.add(summary)
    return summary


@session_context
def _elaborate(update: telegram.Update, context: DbSessionContext, **kwargs) -> Generator[BotMessage, None, None]:
    if update.effective_chat is None:
        raise ValueError("The update must contain a chat.")
    elif not {"transcript_id", "summary_id"} & kwargs.keys():
        raise ValueError("Either transcript_id or summary_id must be given in kwargs.")

    session = context.db_session

    transcript_id = kwargs.get("transcript_id")
    if transcript_id is not None:
        # return the full transcript
        transcript = session.get(Transcript, transcript_id)
        if transcript is None:
            raise ValueError(f"Could not find transcript with id {transcript_id}")
        # if transcript language is not chat language, show a button to translate it
        chat = session.get(TelegramChat, update.effective_chat.id)
        if chat is None:
            raise ValueError(f"Could not find chat with id {update.effective_chat.id}")

        if chat.language != transcript.input_language:
            buttons = [
                InlineKeyboardButton(
                    f"{chat.language.flag_emoji} Translate",
                    callback_data={"fnc": "translate_transcript", "kwargs": {"transcript_id": transcript_id}},
                )
            ]
            markup = InlineKeyboardMarkup([buttons])
        else:
            markup = None

        if len(transcript.result) >= MessageLimit.MAX_TEXT_LENGTH:
            yield BotDocument(
                chat_id=update.effective_chat.id,
                reply_to_message_id=update.effective_message.id,
                filename="transcript.txt",
                document=transcript.result.encode("utf-8"),
            )
        else:
            yield BotMessage(
                chat_id=update.effective_chat.id,
                text=transcript.result,
                reply_markup=markup,
            )
        return

    summary_id = kwargs.get("summary_id")
    summary = session.get(Summary, summary_id)
    if summary is None:
        raise ValueError(f"Could not find summary with id {summary_id}")

    with open(Path(__file__).parent / "data" / "elaborate.txt") as fp:
        system_msg = fp.read().strip()

    topic_str = r"\n".join(f"- {topic.text}" for topic in summary.topics)
    messages = [
        {"role": "system", "content": system_msg},
        {
            "role": "user",
            "content": f"""
Transcript:
{summary.transcript.result}

Topics:
{topic_str}
""",
        },
    ]

    elaboration_result = client.chat.completions.create(model="gpt-4o-mini", messages=messages, temperature=0)
    [choice] = elaboration_result.choices
    chat = session.get(TelegramChat, update.effective_chat.id)
    en_msg = choice.message.content
    if chat is None or chat.language.ietf_tag == "en":
        msg = en_msg
    else:
        deepl_result = translator.translate_text(en_msg, target_lang=chat.language.code)
        msg = deepl_result.text

    for i in range(0, len(msg), MessageLimit.MAX_TEXT_LENGTH):
        yield BotMessage(
            chat_id=update.effective_chat.id,
            text=msg[i : i + MessageLimit.MAX_TEXT_LENGTH],
        )


def get_openai_chatcompletion(messages: list[dict], n_retry: int = 1, max_retries: int = 2) -> dict:
    openai_model = os.getenv("OPENAI_MODEL_ID")
    if openai_model is None:
        raise ValueError("OPENAI_MODEL_ID environment variable not set")
    summary_result = client.chat.completions.create(
        model=openai_model,
        temperature=0,
        messages=messages,
    )
    try:
        [choice] = summary_result.choices
        if choice.message.content.startswith("```"):
            json_str = "\n".join(choice.message.content.splitlines()[1:-1])
        else:
            json_str = choice.message.content
        data = json.loads(json_str)
    except IndexError:
        _logger.error(f"OpenAI returned more than one or no choices: {summary_result}")
        raise
    except json.JSONDecodeError:
        _logger.warning(f"OpenAI returned invalid JSON: {choice.message.content}")
        if n_retry == max_retries:
            raise
        else:
            _logger.info(f"Retrying {n_retry}/{max_retries}")
            messages.append(choice.message)
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Your last message was not valid JSON. Please correct your last answer. "
                        "Your answer should contain nothing but the JSON"
                    ),
                }
            )
            return get_openai_chatcompletion(messages, n_retry + 1, max_retries)
    return data
