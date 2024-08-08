import datetime as dt
import os
from dataclasses import dataclass, field
from typing import Iterator, Optional

import deepl
import telegram
from sqlalchemy import select

from ..models import Language, Topic, TopicTranslation
from ..models.session import DbSessionContext, Session, session_context

__all__ = ["_translate_topic", "_translate_text"]

deepl_token: Optional[str] = os.getenv("DEEPL_TOKEN")
translator = deepl.Translator(deepl_token)


@dataclass
class DeepLLanguage:
    # deepl language code
    # https://www.deepl.com/docs-api/general/get-languages/
    code: str
    name: str
    is_target_lang: bool
    is_source_lang: bool = False
    # https://en.wikipedia.org/wiki/IETF_language_tag
    ietf_language_tag: str = field(init=False)

    def __post_init__(self):
        self.ietf_language_tag = self.code[:2].lower()

    def __str__(self) -> str:
        return f"{self.name}: {self.code} / {self.ietf_language_tag}"


@dataclass
class DeepLLanguages:
    languages: list[DeepLLanguage]
    ietf_tag_to_language: dict[str, DeepLLanguage] = field(init=False)
    lang_code_to_language: dict[str, DeepLLanguage] = field(init=False)

    def __post_init__(self):
        self.languages = [lang for lang in self.languages]
        self.lang_code_to_language = {lang.code: lang for lang in self}
        double_lang_codes = {"en": "EN-US", "pt": "PT-BR"}
        self.ietf_tag_to_language = {
            lang.ietf_language_tag: lang for lang in self if lang.ietf_language_tag not in double_lang_codes
        }
        for ietf_tag, lang in double_lang_codes.items():
            self.ietf_tag_to_language[ietf_tag] = self.lang_code_to_language[lang]

    def __iter__(self) -> Iterator[DeepLLanguage]:
        return iter(self.languages)


available_target_languages = DeepLLanguages(
    [DeepLLanguage(lang.code, lang.name, True) for lang in translator.get_target_languages()]
)


@session_context
def _translate_topic(
    update: telegram.Update, context: DbSessionContext, target_language: Language, topic: Topic
) -> TopicTranslation:
    session = context.db_session
    stmt = (
        select(TopicTranslation)
        .where(TopicTranslation.target_lang == target_language)
        .where(TopicTranslation.topic == topic)
    )

    if translation := session.scalars(stmt).one_or_none():
        return translation

    created_at = dt.datetime.now(dt.UTC)

    source_text = topic.text
    deepl_result = translator.translate_text(source_text, target_lang=target_language.code)

    translation = TopicTranslation(
        created_at=created_at,
        finished_at=dt.datetime.now(dt.UTC),
        topic=topic,
        target_lang=target_language,
        target_text=deepl_result.text,
    )
    session.add(translation)

    return translation


def _translate_text(text: str, target_language: Language) -> str:
    deepl_result = translator.translate_text(text, target_lang=target_language.code)
    return deepl_result.text


def check_database_languages():
    with Session.begin() as session:
        for ietf_tag, lang in available_target_languages.ietf_tag_to_language.items():
            stmt = select(Language).where(Language.ietf_tag == ietf_tag)
            if not session.execute(stmt).one_or_none():
                session.add(
                    Language(
                        name=lang.name,
                        ietf_tag=ietf_tag,
                        code=lang.code,
                    )
                )
