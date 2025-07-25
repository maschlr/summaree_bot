from pathlib import Path
from typing import Optional

from jinja2 import Environment, FileSystemLoader, Template
from telegram import Update
from telegram.helpers import escape_markdown

from ..bot.constants import UI_TRANSLATION_IETF_TAGS

template_root = Path(__file__).parent / "data"
template_paths = [template_root] + [template_root / path for path in UI_TRANSLATION_IETF_TAGS]
env = Environment(loader=FileSystemLoader(template_paths))
env.globals.update(escape_markdown=lambda s: escape_markdown(s, version=2))

TEMPLATES: dict[str, dict[str, str]] = {
    "help": {
        "en": "help.html.jinja2",
        "ru": "help_ru.html.jinja2",
        "es": "help_es.html.jinja2",
        "de": "help_de.html.jinja2",
    },
    "lang": {
        "en": "lang.md.jinja2",
    },
    "lang_prefix": {
        "en": "lang_pre.md.jinja2",
        "ru": "lang_pre_ru.md.jinja2",
        "es": "lang_pre_es.md.jinja2",
        "de": "lang_pre_de.md.jinja2",
    },
    "terms": {
        "en": "terms.md.jinja2",
        "ru": "terms_ru.md.jinja2",
        "es": "terms_es.md.jinja2",
        "de": "terms_de.md.jinja2",
    },
    "sale_suffix": {
        "en": "sale_suffix.md.jinja2",
        "ru": "sale_suffix_ru.md.jinja2",
        "es": "sale_suffix_es.md.jinja2",
        "de": "sale_suffix_de.md.jinja2",
    },
    "token_email": {"en": "token_email.html.jinja2"},
    "register": {"en": "register.md.jinja2"},
    "premium_active": {
        "en": "premium_active.md.jinja2",
        "ru": "premium_active_ru.md.jinja2",
        "es": "premium_active_es.md.jinja2",
        "de": "premium_active_de.md.jinja2",
    },
    "premium_inactive": {
        "en": "premium_inactive.md.jinja2",
        "ru": "premium_inactive_ru.md.jinja2",
        "es": "premium_inactive_es.md.jinja2",
        "de": "premium_inactive_de.md.jinja2",
    },
}


def get_template(name: str, update: Optional[Update] = None) -> Template:
    """
    Returns a specific template
    If no name is given, look for the function name in the stack
    """
    try:
        lang_templates = TEMPLATES[name]
        if update is None:
            template = lang_templates["en"]
        else:
            ietf_tag = update.effective_user.language_code
            template = lang_templates.get(ietf_tag, lang_templates["en"])
    except KeyError as exc:
        raise NotImplementedError(f"Template {name} not found") from exc
    return env.get_template(template)
