import logging

from config import settings

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def get_logger(
    name: str, debug: bool = settings.DEBUG, *, format_string: str | None = None
):
    log_level = logging.DEBUG if debug else logging.INFO
    logger = logging.getLogger(name)
    handler = logging.StreamHandler()
    if format_string:
        formatter = logging.Formatter(format_string)
        handler.setFormatter(formatter)

    logger.setLevel(log_level)
    handler.setLevel(log_level)
    logger.addHandler(handler)
    logger.propagate = False

    return logger
