from pathlib import Path
from typing import Optional

from jinja2 import Environment, FileSystemLoader, Template
from telegram import Update

from ..bot.helpers import escape_markdown

template_root = Path(__file__).parent / "data"
template_paths = [template_root] + [template_root / path for path in ["de", "es", "ru"]]
env = Environment(loader=FileSystemLoader(template_paths))
env.globals.update(escape_markdown=escape_markdown)

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
    "token_email": {"en": "token_email.html.jinja2"},
    "register": {"en": "register.md.jinja2"},
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
