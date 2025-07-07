import asyncio
import datetime as dt
import hashlib
import logging
import os
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Union, cast

import telegram
from openai import AsyncOpenAI, OpenAI
from openai.types.chat import ParsedChatCompletion
from pydantic import BaseModel
from sqlalchemy import func, select
from telegram.ext import ContextTypes

from ..bot.exceptions import EmptyTranscription
from ..models import Language, Summary, Topic, Transcript
from ..models.session import DbSessionContext, Session, session_context
from .audio import split_audio, transcode_ffmpeg

_logger = logging.getLogger(__name__)

client = OpenAI()
aclient = AsyncOpenAI()

__all__ = [
    "transcribe_file",
    "_summarize",
]


async def transcribe_file(
    update: telegram.Update,
    _context: ContextTypes.DEFAULT_TYPE,
    file_path: Path,
    voice_or_audio_or_document_or_video: Union[
        telegram.Voice, telegram.Audio, telegram.Document, telegram.Video, telegram.VideoNote
    ],
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
        or (
            update.message.voice is None
            and update.message.audio is None
            and update.message.document is None
            and update.message.video is None
            and update.message.video_note is None
        )
        or update.effective_user is None
    ):
        raise ValueError("The update must contain a voice or audio or a video message(and an effective_user).")
    voice_or_audio_or_document_or_video = cast(
        Union[telegram.Voice, telegram.Audio, telegram.Document, telegram.Video, telegram.VideoNote],
        (
            update.message.voice
            or update.message.audio
            or update.message.document
            or update.message.video
            or update.message.video_note
        ),
    )

    # convert the unsupported file (e.g. .ogg for normal voice) to .mp3
    if file_path.suffix[1:] not in {
        "flac",
        "mp3",
        "mp4",
        "mpga",
        "m4a",
        "ogg",
        "wav",
        "webm",
    } or (update.message.video or update.message.video_note):
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
            file_unique_id=voice_or_audio_or_document_or_video.file_unique_id,
            file_id=voice_or_audio_or_document_or_video.file_id,
            sha256_hash=sha256_hash,
            duration=voice_or_audio_or_document_or_video.duration
            if hasattr(voice_or_audio_or_document_or_video, "duration")
            else None,
            mime_type=voice_or_audio_or_document_or_video.mime_type,
            file_size=voice_or_audio_or_document_or_video.file_size,
            result=whisper_transcription.text,
            input_language=transcript_language,
            total_seconds=whisper_transcription.total_seconds,
        )
        session.add(transcript)
        session.flush()
        return transcript


@dataclass
class WhisperTranscription:
    text: str
    language: str
    total_seconds: int


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
    total_seconds = 0
    for task in tasks:
        transcription_result = task.result()
        total_seconds += int(round(transcription_result.model_extra.get("duration", 0), 0))
        languages.append(transcription_result.model_extra.get("language"))
        texts.append(transcription_result.text)

    language_counter = Counter(languages)
    try:
        most_common_language = language_counter.most_common(1)[0][0]
    except IndexError:
        _logger.warning("Could not determine language of the transcription")
        most_common_language = None

    result = WhisperTranscription(text="\n".join(texts), language=most_common_language, total_seconds=total_seconds)

    if temp_dir is not None:
        temp_dir.cleanup()

    if not result.text:
        raise EmptyTranscription("Whisper transcription failed (empty response)")

    return result


async def get_whisper_transcription_async(file_path: Path):
    with open(file_path, "rb") as fp:
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

    created_at = dt.datetime.now(dt.UTC)
    openai_response: ParsedChatCompletion = get_openai_chatcompletion(transcript.result)
    [choice] = openai_response.choices
    summary_response: SummaryResponse = choice.message.parsed

    if transcript.input_language is None or transcript.input_language.ietf_tag != summary_response.ietf_language_tag:
        language_stmt = select(Language).where(Language.ietf_tag == summary_response.ietf_language_tag)
        if not (language := session.scalars(language_stmt).one_or_none()):
            _logger.warning(f"Could not find language with ietf_tag {summary_response.ietf_language_tag}")
        else:
            transcript.input_language = language

    summary = Summary(
        created_at=created_at,
        finished_at=dt.datetime.now(dt.UTC),
        transcript=transcript,
        topics=[Topic(text=text, order=i) for i, text in enumerate(summary_response.topics, start=1)],
        tg_user_id=update.effective_user.id,
        tg_chat_id=update.effective_chat.id,
        openai_id=openai_response.id,
        openai_model=openai_response.model,
        completion_tokens=openai_response.usage.completion_tokens,
        prompt_tokens=openai_response.usage.prompt_tokens,
    )
    transcript.reaction_emoji = summary_response.emoji
    transcript.hashtags = summary_response.hashtags

    session.add(summary)
    return summary


class SummaryResponse(BaseModel):
    ietf_language_tag: Literal[
        "bg",
        "cs",
        "da",
        "de",
        "el",
        "es",
        "et",
        "fi",
        "fr",
        "hu",
        "id",
        "it",
        "ja",
        "ko",
        "lt",
        "lv",
        "nb",
        "nl",
        "pl",
        "ro",
        "ru",
        "sk",
        "sl",
        "sv",
        "tr",
        "uk",
        "zh",
        "en",
        "pt",
    ]
    topics: list[str]
    emoji: Literal[
        "THUMBS_UP",
        "THUMBS_DOWN",
        "RED_HEART",
        "FIRE",
        "SMILING_FACE_WITH_HEARTS",
        "CLAPPING_HANDS",
        "GRINNING_FACE_WITH_SMILING_EYES",
        "THINKING_FACE",
        "SHOCKED_FACE_WITH_EXPLODING_HEAD",
        "FACE_SCREAMING_IN_FEAR",
        "SERIOUS_FACE_WITH_SYMBOLS_COVERING_MOUTH",
        "CRYING_FACE",
        "PARTY_POPPER",
        "GRINNING_FACE_WITH_STAR_EYES",
        "FACE_WITH_OPEN_MOUTH_VOMITING",
        "PILE_OF_POO",
        "PERSON_WITH_FOLDED_HANDS",
        "OK_HAND_SIGN",
        "DOVE_OF_PEACE",
        "CLOWN_FACE",
        "YAWNING_FACE",
        "FACE_WITH_UNEVEN_EYES_AND_WAVY_MOUTH",
        "SMILING_FACE_WITH_HEART_SHAPED_EYES",
        "SPOUTING_WHALE",
        "HEART_ON_FIRE",
        "NEW_MOON_WITH_FACE",
        "HOT_DOG",
        "HUNDRED_POINTS_SYMBOL",
        "ROLLING_ON_THE_FLOOR_LAUGHING",
        "HIGH_VOLTAGE_SIGN",
        "BANANA",
        "TROPHY",
        "BROKEN_HEART",
        "FACE_WITH_ONE_EYEBROW_RAISED",
        "NEUTRAL_FACE",
        "STRAWBERRY",
        "BOTTLE_WITH_POPPING_CORK",
        "KISS_MARK",
        "REVERSED_HAND_WITH_MIDDLE_FINGER_EXTENDED",
        "SMILING_FACE_WITH_HORNS",
        "SLEEPING_FACE",
        "LOUDLY_CRYING_FACE",
        "NERD_FACE",
        "GHOST",
        "MAN_TECHNOLOGIST",
        "EYES",
        "JACK_O_LANTERN",
        "SEE_NO_EVIL_MONKEY",
        "SMILING_FACE_WITH_HALO",
        "FEARFUL_FACE",
        "HANDSHAKE",
        "WRITING_HAND",
        "HUGGING_FACE",
        "SALUTING_FACE",
        "FATHER_CHRISTMAS",
        "CHRISTMAS_TREE",
        "SNOWMAN",
        "NAIL_POLISH",
        "GRINNING_FACE_WITH_ONE_LARGE_AND_ONE_SMALL_EYE",
        "MOYAI",
        "SQUARED_COOL",
        "HEART_WITH_ARROW",
        "HEAR_NO_EVIL_MONKEY",
        "UNICORN_FACE",
        "FACE_THROWING_A_KISS",
        "PILL",
        "SPEAK_NO_EVIL_MONKEY",
        "SMILING_FACE_WITH_SUNGLASSES",
        "ALIEN_MONSTER",
        "MAN_SHRUGGING",
        "SHRUG",
        "WOMAN_SHRUGGING",
        "POUTING_FACE",
    ]
    hashtags: list[str]


def get_openai_chatcompletion(transcript: str) -> ParsedChatCompletion:
    openai_model = os.getenv("OPENAI_MODEL_ID")
    if openai_model is None:
        raise ValueError("OPENAI_MODEL_ID environment variable not set")

    prompt = (
        "You are an advanced AI assistant for analyzing voice messages and audio files. "
        "You will be given a transcript of a voice message or audio file in a language you can understand,"
        " and you need to extract the following information:\n"
        "1. The language of the message using the IETF language tag format (e.g. 'en', 'de', 'zh', etc.)\n"
        "2. The topics discussed in the message. The topics should be written in the language of the transcript."
        " Write them as bullet points, each topic described in a concise but complete sentence.\n"
        "3. ONE emoji that best describes the message.\n"
        "4. UP TO THREE hashtags that best describe the message. Make sure to prefix them with ONLY with a '#' symbol."
    )

    summary_result = client.beta.chat.completions.parse(
        model=openai_model,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": transcript},
        ],
        response_format=SummaryResponse,
        n=1,
    )

    return summary_result
