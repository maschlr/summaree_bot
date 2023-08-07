import hashlib
import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Optional, Union, cast

import openai
import telegram
from sqlalchemy import select

from ..bot import BotMessage, ensure_chat
from ..models import Language, Summary, TelegramChat, Topic, Transcript
from ..models.session import DbSessionContext, session_context
from .deepl import _translate

_logger = logging.getLogger(__name__)

mimetype_pattern = re.compile(r"(?P<type>\w+)/(?P<subtype>\w+)")

__all__ = [
    "_check_existing_transcript",
    "_extract_file_name",
    "_transcribe_file",
    "_summarize",
    "_get_summary_message",
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

    if update.message is None or (update.message.voice is None and update.message.audio is None):
        raise ValueError("The update must contain a voice or audio message.")
    voice_or_audio = cast(Union[telegram.Voice, telegram.Audio], (update.message.voice or update.message.audio))

    # convert the unsupported file (e.g. .ogg for normal voice) to .mp3
    if file_path.suffix[1:] not in {
        "mp3",
        "mp4",
        "mpeg",
        "mpga",
        "m4a",
        "wav",
        "webm",
    }:
        supported_file_path = transcode_to_mp3(file_path)
    else:
        supported_file_path = file_path

    # send the .mp3 file to openai whisper, create a db entry and return it
    with open(supported_file_path, "rb") as mp3_fp:
        transcription_result = openai.Audio.transcribe("whisper-1", mp3_fp)

    transcript = Transcript(
        file_unique_id=voice_or_audio.file_unique_id,
        file_id=voice_or_audio.file_id,
        sha256_hash=sha256_hash,
        duration=voice_or_audio.duration,
        mime_type=voice_or_audio.mime_type,
        file_size=voice_or_audio.file_size,
        result=transcription_result["text"],
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

    with open(Path(__file__).parent / "data" / "prompt.txt") as fp:
        system_msgs = [line.strip() for line in fp.readlines()]

    user_message = transcript.result
    messages = [
        *[{"role": "system", "content": system_msg} for system_msg in system_msgs],
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
def _get_summary_message(update: telegram.Update, context: DbSessionContext, summary: Summary) -> BotMessage:
    if update.effective_chat is None:
        raise ValueError("The update must contain a chat.")

    session = context.db_session
    session.add(summary)
    chat = session.get(TelegramChat, update.effective_chat.id)
    if chat is None:
        raise ValueError(f"Could not find chat with id {update.effective_chat.id}")

    en_lang = Language.get_default_language(session)
    if chat.language != en_lang:
        translations = [
            _translate(session=session, target_language=chat.language, topic=topic) for topic in summary.topics
        ]
        session.add_all(translations)
        msg = "\n".join(f"- {translation.target_text}" for translation in translations)
    else:
        msg = "\n".join(f"- {topic.text}" for topic in summary.topics)

    return BotMessage(update.effective_chat.id, msg)


def get_openai_chatcompletion(messages: list[dict], n_retry: int = 1, max_retries: int = 2) -> dict:
    summary_result = openai.ChatCompletion.create(
        model="gpt-3.5-turbo-0613",
        messages=messages,
    )
    try:
        [choice] = summary_result["choices"]
        data = json.loads(choice["message"]["content"])
    except IndexError:
        _logger.error(f"OpenAI returned more than one or no choices: {summary_result}")
        raise
    except json.JSONDecodeError:
        _logger.warning(f'OpenAI returned invalid JSON: {choice["message"]["content"]}')
        if n_retry == max_retries:
            raise
        else:
            _logger.info(f"Retrying {n_retry}/{max_retries}")
            messages.append(choice["message"])
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
