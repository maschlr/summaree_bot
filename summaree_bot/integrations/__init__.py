import dotenv
dotenv.load_dotenv()
from .openai import transcribe, summarize
from .deepl import translate, check_database_languages
