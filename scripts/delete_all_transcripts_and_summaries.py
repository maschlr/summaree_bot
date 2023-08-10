from sqlalchemy import select

from summaree_bot.models import Transcript
from summaree_bot.models.session import Session

if __name__ == "__main__":
    with Session.begin() as session:
        transcripts = session.scalars(select(Transcript))
        for transcript in transcripts:
            session.delete(transcript)
