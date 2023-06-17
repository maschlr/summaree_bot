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

from ..models import Transcript, Summary, Topic

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
        return transcript

    # create a temporary folder
    with tempfile.TemporaryDirectory() as tempdir_path_str:
        # download the .ogg file to the folder
        tempdir_path = Path(tempdir_path_str)
        file_path = tempdir_path / f"{update.message.voice.file_unique_id}.ogg"
        file = await update.message.voice.get_file()
        await file.download_to_drive(file_path)

        with open(file_path, "rb") as fp:
            m = hashlib.sha256()
            while chunk := fp.read(8192):
                m.update(chunk)
            sha256_hash = m.hexdigest()
            stmt = select(Transcript).where(Transcript.sha256_hash == sha256_hash)
            if transcript := session.scalars(stmt).one_or_none():
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
                file_unique_id=update.message.voice.file_unique_id,
                file_id=update.message.voice.file_id,
                sha256_hash=sha256_hash,
                duration=update.message.voice.duration,
                mime_type=update.message.voice.mime_type,
                file_size=update.message.voice.file_size,
                result=transcription_result["text"]
            )
            session.add(transcript)
            return transcript

def summarize(transcript: Transcript, session: Session) -> Summary:
    # if the transcript is already summarized return it
    stmt = select(Summary).where(Summary.transcript == transcript)
    if summary := session.scalars(stmt).one_or_none():
        return summary

    system_msgs = (
        "You will receive transcriptios of voice messages in different input languages. ",
        "Detect and output the input language as en ietf language tag. ",
        "Your task is to translate the transcript into english and summarize it in bullet points. ",
        "In the first step translate the transcript of the message into english, cleaning up the text and removing any filler words. ",
        "In the second step, summarize the message in your own words in bullet points while keeping the meaning and the context. ",
        "Group similar bullet points by topic together. Only output the final result. ",
        "Your answer should be a valid JSON string and nothing else.", 
        "The JSON object should have the following structure: {\"language\": \"<ietf_language_tag>\", \"topics\": [\"<bullet_point1>\", \"<bullet_point2>\"]}"
    )

    user_message = transcript.result

    summary_result = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[
            *[{"role": "system", "content": system_msg} for system_msg in system_msgs],
            {"role": "user", "content": user_message}
        ]
    )

    [choice] = summary_result["choices"]
    data = json.loads(choice["message"]["content"])
    summary = Summary(
        transcript = transcript,
        topics = [Topic(text=text) for text in data["topics"]],
    )
    session.add(summary)

    return summary
