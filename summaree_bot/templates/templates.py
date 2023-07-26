from pathlib import Path

from jinja2 import Environment, FileSystemLoader

template_path = Path(__file__).parent / "data"
env = Environment(loader=FileSystemLoader(template_path))

TEMPLATES: dict[str, str] = {
    "start": "start.md.jinja2",
    "help": "help.md.jinja2",
    "token_email": "token_email.html.jinja2",
    "register": "register.md.jinja2",
}


def get_template(name: str):
    try:
        template = TEMPLATES[name]
    except KeyError:
        raise ValueError(f"Template {name} not found")
    return env.get_template(template)
