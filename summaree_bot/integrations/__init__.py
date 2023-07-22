import dotenv

dotenv.load_dotenv()
from .deepl import check_database_languages, translate
from .openai import summarize, transcribe_audio, transcribe_voice
