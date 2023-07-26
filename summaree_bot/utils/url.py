import base64
import json


def encode(data: object) -> bytes:
    """Encode a list of strings into a single string."""
    json_str = json.dumps(data)
    return base64.urlsafe_b64encode(json_str.encode("utf-8"))


def decode(data: str) -> object:
    """Decode a single string into a list of strings."""
    json_str = base64.urlsafe_b64decode(data).decode("utf-8")
    return json.loads(json_str)
