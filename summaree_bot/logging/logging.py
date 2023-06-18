import logging
from pathlib import Path

LOGGING_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
formatter = logging.Formatter()
logging.basicConfig(
    format=LOGGING_FORMAT, level=logging.INFO
)
root_dir = Path(__file__).parent
fh = logging.FileHandler(root_dir / "summaree_bot.log")
fh.setFormatter(formatter)
logging.getLogger().addHandler(fh)
