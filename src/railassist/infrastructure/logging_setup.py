import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from railassist.domain.models import utc_now


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Explicit allowlist: no arbitrary payload, account info or config.
        return json.dumps({
            "time": utc_now(), "level": record.levelname,
            "event": getattr(record, "event", "application"),
            "task_id": getattr(record, "task_id", None),
        }, ensure_ascii=False)


def setup_logging(directory: Path) -> logging.Logger:
    directory.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("railassist")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for old in logger.handlers[:]:
        old.close()
        logger.removeHandler(old)
    handler = RotatingFileHandler(directory / "app.jsonl", maxBytes=10 * 1024 * 1024, backupCount=9, encoding="utf-8")
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    return logger

