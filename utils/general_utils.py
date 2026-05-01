import logging
import sentry_sdk

from config import settings

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def init_sentry():
    if settings.sentry.REPORT:
        logger.info("Inicializando o sentry")
        sentry_sdk.init(
            f"https://{settings.sentry.PUBLIC_KEY}@{settings.sentry.ADDRESS}",
            # Set traces_sample_rate to 1.0 to capture 100%
            # of transactions for performance monitoring.
            # We recommend adjusting this value in production.
            traces_sample_rate=settings.sentry.TRACES_SAMPLE_RATE,
        )
    else:
        logger.info("Não carregar o sentry")


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
