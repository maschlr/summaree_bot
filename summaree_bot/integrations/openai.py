import hashlib
import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Optional, Union, cast

import telegram
from openai import OpenAI
from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ..bot import BotMessage, ensure_chat
from ..models import Language, Summary, TelegramChat, Topic, Transcript
from ..models.session import DbSessionContext, session_context
from .deepl import translator

_logger = logging.getLogger(__name__)

mimetype_pattern = re.compile(r"(?P<type>\w+)/(?P<subtype>\w+)")
summary_prompt_file_path = Path(__file__).parent / "data" / "summarize.txt"
client = OpenAI()

__all__ = [
    "_check_existing_transcript",
    "_extract_file_name",
    "_transcribe_file",
    "_summarize",
]


def transcode_to_mp3(file_path: Path) -> Path:
    _logger.info(f"Transcoding file: {file_path}")
    # convert the .ogg file to .mp3 using ffmpeg

    mp3_filepath = file_path.parent / f"{file_path.stem}.mp3"
    run_args = ["ffmpeg", "-i", str(file_path), "-f", "mp3", str(mp3_filepath)]
    subprocess.run(run_args, capture_output=True)
    _logger.info(f"Transcoding successfully finished: {mp3_filepath}")
    return mp3_filepath


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


@session_context
def _transcribe_file(
    update: telegram.Update,
    context: DbSessionContext,
    file_path: Path,
    voice_or_audio: Union[telegram.Voice, telegram.Audio],
) -> Transcript:
    session = context.db_session
    with open(file_path, "rb") as fp:
        m = hashlib.sha256()
        while chunk := fp.read(8192):
            m.update(chunk)
        sha256_hash = m.hexdigest()
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
        supported_file_path = transcode_to_mp3(file_path)
    else:
        supported_file_path = file_path

    # send the .mp3 file to openai whisper, create a db entry and return it
    with open(supported_file_path, "rb") as fp:
        transcription_result = client.audio.transcriptions.create(model="whisper-1", file=fp, response_format="text")

    transcript = Transcript(
        file_unique_id=voice_or_audio.file_unique_id,
        file_id=voice_or_audio.file_id,
        sha256_hash=sha256_hash,
        duration=voice_or_audio.duration,
        mime_type=voice_or_audio.mime_type,
        file_size=voice_or_audio.file_size,
        result=transcription_result,
        tg_user_id=update.effective_user.id,
        tg_chat_id=update.effective_chat.id,
    )
    session.add(transcript)
    return transcript


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

    summary_data = get_openai_chatcompletion(messages)

    # TODO: translation logic: premium feature
    language_stmt = select(Language).where(Language.ietf_tag == summary_data["language"])
    if not (language := session.scalars(language_stmt).one_or_none()):
        _logger.warning(f"Could not find language with ietf_tag {summary_data['language']}")
    else:
        transcript.input_language = language

    summary = Summary(
        transcript=transcript,
        topics=[Topic(text=text) for text in summary_data["topics"]],
    )
    session.add(summary)
    return summary


@session_context
def _elaborate(update: telegram.Update, context: DbSessionContext, **kwargs) -> BotMessage:
    if update.effective_chat is None:
        raise ValueError("The update must contain a chat.")
    elif not {"transcript_id", "summary_id"} & kwargs.keys():
        raise ValueError("Either transcript_id or summary_id must be given in kwargs.")

    session = context.db_session

    transcript_id = kwargs.get("transcript_id")
    if transcript_id is not None:
        transcript = session.get(Transcript, transcript_id)
        if transcript is None:
            raise ValueError(f"Could not find transcript with id {transcript_id}")
        # if transcript language is not chat language, show a button to translate it
        chat = session.get(TelegramChat, update.effective_chat.id)
        if chat is None:
            raise ValueError(f"Could not find chat with id {update.effective_chat.id}")
        if chat.language == transcript.input_language:
            return BotMessage(chat_id=update.effective_chat.id, text=transcript.result)

        buttons = [
            InlineKeyboardButton(
                f"{chat.language.flag_emoji} Translate",
                callback_data={"fnc": "translate_transcript", "kwargs": {"transcript_id": transcript_id}},
            )
        ]
        return BotMessage(
            chat_id=update.effective_chat.id, text=transcript.result, reply_markup=InlineKeyboardMarkup([buttons])
        )

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

    elaboration_result = client.chat.completions.create(model="gpt-3.5-turbo-0613", messages=messages, temperature=0)
    [choice] = elaboration_result.choices
    chat = session.get(TelegramChat, update.effective_chat.id)
    en_msg = choice.message.content
    if chat is None or chat.language.ietf_tag == "en":
        msg = en_msg
    else:
        deepl_result = translator.translate_text(en_msg, target_lang=chat.language.code)
        msg = deepl_result.text
    # TODO: this might generate messages that are too long; handle that case
    return BotMessage(chat_id=update.effective_chat.id, text=msg)


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
        data = json.loads(choice.message.content)
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
