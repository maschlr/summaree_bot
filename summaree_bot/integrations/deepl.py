import os
from dataclasses import dataclass, field
from typing import Iterator, Optional

import deepl
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Language, Topic, Translation
from ..models.session import add_session

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


def translate(session: Session, target_language: Language, topic: Topic) -> Translation:
    stmt = select(Translation).where(Translation.target_lang == target_language).where(Translation.topic == topic)

    if translation := session.scalars(stmt).one_or_none():
        return translation

    source_text = topic.text
    deepl_result = translator.translate_text(source_text, target_lang=target_language.code)

    translation = Translation(
        topic=topic,
        target_lang=target_language,
        target_text=deepl_result.text,
    )
    session.add(translation)

    return translation


@add_session
def check_database_languages(session: Session):
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
