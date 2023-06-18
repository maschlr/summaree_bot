from pathlib import Path
import uuid

from sqlalchemy import select

from summaree_bot.integrations.openai import transcribe_file, summarize
from summaree_bot.models import Transcript, Summary

from .common import Common

class MockVoice:
    def __init__(self):
        self.file_id = uuid.uuid4().hex
        self.file_unique_id = uuid.uuid4().hex[:8]
        self.mime_type = "audio/ogg"
        self.file_size = 123456


class TestOpenAI(Common):
    @classmethod
    def setUpClass(cls):
        result = super().setUpClass()

        # create transcripts
        # TODO: instead of transcribing the files via the api, use already produced transcrips from the test data
        data_dir = Path(__file__).parents[3] / "research/data"
        cls.mp3_file_paths = list(data_dir.glob("*.mp3"))
        voices = [MockVoice() for _ in cls.mp3_file_paths]

        with cls.Session.begin() as session:
            transcripts = [transcribe_file(file_path, voice, session) for file_path, voice in zip(cls.mp3_file_paths, voices)]

        return result

    def test_00_create_summaries(self):
        with self.Session.begin() as session:
            transcripts = session.scalars(select(Transcript)).all()
            self.assertEqual(len(transcripts), len(self.mp3_file_paths))

            summaries = [summarize(transcript, session) for transcript in transcripts]

        with self.Session.begin() as session:
            summaries = session.scalars(select(Summary)).all()
            self.assertEqual(len(summaries), len(self.mp3_file_paths))

    def test_01_yield_summaries_from_already_summarized_transcripts(self):
        # count the number of summaries in the db
        with self.Session.begin() as session:
            summaries = self.Session.scalars(select(Summary)).all()
            num_summaries = len(summaries)
            self.assertEqual(num_summaries, len(self.mp3_file_paths))

            # call summarize() with the transcripts in the db
            for transcript in session.scalars(select(Transcript)).all():
                summary = summarize(transcript, session)
                self.assertTrue(summary in summaries)
            
            # assert that the number of summaries in the db is the same
            summaries = self.Session.scalars(select(Summary)).all()
            self.assertEqual(len(summaries), num_summaries)
