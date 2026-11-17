import logging
from contextlib import contextmanager
from unittest import mock


class _Root(logging.Handler):
    def emit(self, record):
        pass


@contextmanager
def capture_handled_records(level=logging.INFO):
    """Every record a handler receives, from any logger, at a production level.

    The root and ``openIMIS`` loggers are set to ``level`` (DJANGO_LOG_LEVEL in
    production); a record that only a NullHandler or no handler at all would see
    is not captured, as it is written nowhere.
    """
    records = {}
    handle = logging.Handler.handle

    def capture(handler, record):
        records[id(record)] = record
        return handle(handler, record)

    configured = [logging.getLogger(), logging.getLogger("openIMIS")]
    levels = [logger.level for logger in configured]
    root_handler = _Root()
    logging.getLogger().addHandler(root_handler)
    for logger in configured:
        logger.setLevel(level)
    try:
        with mock.patch.object(logging.Handler, "handle", capture):
            yield records
    finally:
        logging.getLogger().removeHandler(root_handler)
        for logger, previous in zip(configured, levels):
            logger.setLevel(previous)


def formatted(record):
    return logging.Formatter("%(levelname)s %(name)s: %(message)s").format(record)
