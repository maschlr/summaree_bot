from pathlib import Path
from typing import Optional

from jinja2 import Environment, FileSystemLoader, Template
from telegram import Update

template_root = Path(__file__).parent / "data"
template_paths = [template_root] + [template_root / path for path in ["de", "es", "ru"]]
env = Environment(loader=FileSystemLoader(template_paths))

TEMPLATES: dict[str, dict[str, str]] = {
    "help": {
        "en": "help.html.jinja2",
        "ru": "help_ru.html.jinja2",
        "es": "help_es.html.jinja2",
        "de": "help_de.html.jinja2",
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
