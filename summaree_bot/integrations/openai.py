import hashlib
import json
import logging
import re
import subprocess
import tempfile
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Coroutine, Union

import magic
import openai
import telegram
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Language, Summary, Topic, Transcript

_logger = logging.getLogger(__name__)

mimetype_pattern = re.compile(r"(?P<type>\w+)/(?P<subtype>\w+)")


def transcode_to_mp3(file_path: Path) -> Path:
    _logger.info(f"Transcoding file: {file_path}")
    # convert the .ogg file to .mp3 using ffmpeg

    mp3_filepath = file_path.parent / f"{file_path.stem}.mp3"
    run_args = ["ffmpeg", "-i", str(file_path), "-f", "mp3", str(mp3_filepath)]
    subprocess.run(run_args, capture_output=True)
    _logger.info(f"Transcoding successfully finished: {mp3_filepath}")
    return mp3_filepath


def check_file_unique_id(
    fnc,
) -> Callable[[telegram.Update, Session], Coroutine[Any, Any, Transcript]]:
    @wraps(fnc)
    async def wrapper(update: telegram.Update, session: Session) -> Transcript:
        if update.message is None:
            raise ValueError("The update must contain a message.")

        for voice_or_audio in (update.message.voice, update.message.audio):
            if voice_or_audio is not None:
                file_unique_id = voice_or_audio.file_unique_id
                break

        stmt = select(Transcript).where(Transcript.file_unique_id == file_unique_id)
        if transcript := session.scalars(stmt).one_or_none():
            _logger.info(f"Using already existing transcript: {transcript} with file_unique_id: {file_unique_id}")
            return transcript
        else:
            return await fnc(update, session)

    return wrapper


@check_file_unique_id
async def transcribe_audio(update: telegram.Update, session: Session) -> Transcript:
    if not update.message or not update.message.audio:
        raise ValueError("The update must contain an audio message.")

    file_name = update.message.audio.file_name or update.message.audio.file_unique_id
    return await transcribe_file(file_name, update.message.audio, session)


@check_file_unique_id
async def transcribe_voice(update: telegram.Update, session: Session) -> Transcript:
    if not update.message or not update.message.voice:
        raise ValueError("The update must contain a voice message.")

    match = None
    if mime_type := update.message.voice.mime_type:
        match = mimetype_pattern.match(mime_type)

    if match is None:
        file_name = update.message.voice.file_unique_id
    else:
        file_name = f"{update.message.voice.file_unique_id}.{match.group('subtype')}"

    return await transcribe_file(file_name, update.message.voice, session)


async def transcribe_file(
    file_name_str: str,
    voice_or_audio: Union[telegram.Voice, telegram.Audio],
    session: Session,
) -> Transcript:
    if voice_or_audio is None:
        raise ValueError(
            "Arg voice_or_audio is None. Can only transcribe when getting passed a voice or audio message."
        )

    file_name = Path(file_name_str)

    # create a temporary folder
    with tempfile.TemporaryDirectory() as tempdir_path_str:
        # download the file to the folder
        tempdir_path = Path(tempdir_path_str)
        file_path = tempdir_path / file_name
        file = await voice_or_audio.get_file()
        await file.download_to_drive(file_path)

        if not file_name.suffix:
            mime = magic.from_file(file_path, mime=True)
            _, suffix = mime.split("/")
            file_path.rename(file_path.with_suffix(f".{suffix}"))

        transcript = _transcribe_file(file_path, voice_or_audio, session)
        return transcript


def _transcribe_file(
    file_path: Path, voice_or_audio: Union[telegram.Voice, telegram.Audio], session: Session, commit: bool = False
) -> Transcript:
    with open(file_path, "rb") as fp:
        m = hashlib.sha256()
        while chunk := fp.read(8192):
            m.update(chunk)
        sha256_hash = m.hexdigest()
        stmt = select(Transcript).where(Transcript.sha256_hash == sha256_hash)
        if transcript := session.execute(stmt).scalar_one_or_none():
            _logger.info(f"Using already existing transcript: {transcript} with sha256_hash: {sha256_hash}")
            return transcript

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
        if commit:
            session.commit()
        return transcript


def summarize(transcript: Transcript, session: Session, commit: bool = True) -> Summary:
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

    language_stmt = select(Language).where(Language.ietf_tag == summary_data["language"])
    if not (language := session.scalars(language_stmt).one_or_none()):
        _logger.warning(f"Could not find language with ietf_tag {summary_data['language']}")
    else:
        transcript.input_language = language
        session.add(transcript)

    summary = Summary(
        transcript=transcript,
        topics=[Topic(text=text) for text in summary_data["topics"]],
    )
    session.add(summary)
    if commit:
        session.commit()

    return summary


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
