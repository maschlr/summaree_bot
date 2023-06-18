from pathlib import Path
import logging
import subprocess
import tempfile
import hashlib
import json

import openai
import telegram
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Transcript, Summary, Topic, Language

_logger = logging.getLogger(__name__)


def transcode_to_mp3(file_path: Path) -> Path:
    _logger.info(f'Transcoding file: {file_path}')
    # convert the .ogg file to .mp3 using ffmpeg
    
    mp3_filepath = file_path.parent / f"{file_path.stem}.mp3"
    run_args = ["ffmpeg", "-i", str(file_path), "-f", "mp3", str(mp3_filepath)]
    ffmpeg_result = subprocess.run(run_args, capture_output=True)
    _logger.info(f'Transcoding successfully finished: {mp3_filepath}')
    return mp3_filepath


async def transcribe(update: telegram.Update, session: Session) -> Transcript:
    if update.message is None or update.message.voice is None:
        raise ValueError("The update must contain a voice message.")

    stmt = select(Transcript).where(Transcript.file_unique_id == update.message.voice.file_unique_id)
    if (transcript := session.scalars(stmt).one_or_none()):
        _logger.info(f"Using already existing transcript: {transcript} with file_unique_id: {update.message.voice.file_unique_id}")
        return transcript

    # create a temporary folder
    with tempfile.TemporaryDirectory() as tempdir_path_str:
        # download the .ogg file to the folder
        tempdir_path = Path(tempdir_path_str)
        file_path = tempdir_path / f"{update.message.voice.file_unique_id}.ogg"
        file = await update.message.voice.get_file()
        await file.download_to_drive(file_path)

        return transcribe_file(file_path, update.message.voice, session)

def transcribe_file(file_path: Path, voice: telegram.Voice, session: Session) -> Transcript:
    with open(file_path, "rb") as fp:
        m = hashlib.sha256()
        while chunk := fp.read(8192):
            m.update(chunk)
        sha256_hash = m.hexdigest()
        stmt = select(Transcript).where(Transcript.sha256_hash == sha256_hash)
        if transcript := session.scalars(stmt).one_or_none():
            _logger.info(f"Using already existing transcript: {transcript} with sha256_hash: {sha256_hash}")
            return transcript
    
    # convert the .ogg file to .mp3
    if file_path.suffix != "mp3":
        mp3_file_path = transcode_to_mp3(file_path)
    else:
        mp3_file_path = file_path
        
    # send the .mp3 file to openai whisper, create a db entry and return it
    with open(mp3_file_path, "rb") as mp3_fp:
        transcription_result = openai.Audio.transcribe("whisper-1", mp3_fp)
        transcript = Transcript(
            file_unique_id=voice.file_unique_id,
            file_id=voice.file_id,
            sha256_hash=sha256_hash,
            duration=voice.duration,
            mime_type=voice.mime_type,
            file_size=voice.file_size,
            result=transcription_result["text"]
        )
        session.add(transcript)
        session.commit()
        return transcript

def summarize(transcript: Transcript, session: Session) -> Summary:
    # if the transcript is already summarized return it
    if transcript.summary is not None:
        return transcript.summary

    with open(Path(__file__).parent / "data" / "prompt.txt") as fp:
        system_msgs = [line.strip() for line in fp.readlines()]

    user_message = transcript.result
    messages = [
        *[{"role": "system", "content": system_msg} for system_msg in system_msgs],
        {"role": "user", "content": user_message}
    ]

    summary_data = get_openai_chatcompletion(messages)
    
    language_stmt = select(Language).where(Language.ietf_tag == summary_data["language"])
    if not (language := session.scalars(language_stmt).one_or_none()):
        _logger.warning(f"Could not find language with ietf_tag {summary_data['language']}")
    else:
        transcript.input_language = language
        session.add(transcript)

    summary = Summary(
        transcript = transcript,
        topics = [Topic(text=text) for text in summary_data["topics"]],
    )
    session.add(summary)
    session.commit()

    return summary

def get_openai_chatcompletion(messages: list[dict], n_retry: int=1, max_retries: int=2) -> dict:
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
            messages.append({"role": "user", "content": "Your last message was not valid JSON. Please correct your last answer. Your answer should contain nothing but the JSON"})
            return get_openai_chatcompletion(messages, n_retry + 1, max_retries)
    return data
