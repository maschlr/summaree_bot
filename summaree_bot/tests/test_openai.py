import uuid
from pathlib import Path

from sqlalchemy import select

from summaree_bot.integrations.openai import summarize, transcribe_file
from summaree_bot.models import Summary, Transcript

from .common import Common


class MockVoice:
    def __init__(self):
        self.file_id = uuid.uuid4().hex
        self.file_unique_id = uuid.uuid4().hex[:8]
        self.mime_type = "audio/ogg"
        self.file_size = 123456
        self.duration = 42


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
            for file_path, voice in zip(cls.mp3_file_paths, voices, strict=True):
                transcribe_file(file_path, voice, session, commit=False)

        return result

    def test_00_create_summaries(self):
        with self.Session.begin() as session:
            transcripts = session.scalars(select(Transcript)).all()
            self.assertEqual(len(transcripts), len(self.mp3_file_paths))
            for transcript in transcripts:
                summary = summarize(transcript, session, commit=False)
                self.assertIsNotNone(summary)
            session.commit()

        with self.Session.begin() as session:
            summaries = session.scalars(select(Summary)).all()
            self.assertEqual(len(summaries), len(self.mp3_file_paths))

    def test_01_yield_summaries_from_already_summarized_transcripts(self):
        # count the number of summaries in the db
        with self.Session.begin() as session:
            summaries = session.scalars(select(Summary)).all()
            num_summaries = len(summaries)
            self.assertEqual(num_summaries, len(self.mp3_file_paths))

            # call summarize() with the transcripts in the db
            for transcript in session.scalars(select(Transcript)).all():
                summary = summarize(transcript, session)
                self.assertTrue(summary in summaries)

        # assert that the number of summaries in the db is the same
        with self.Session.begin() as session:
            summaries = session.scalars(select(Summary)).all()
            self.assertEqual(len(summaries), num_summaries)
