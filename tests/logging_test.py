import importlib
import logging
from io import StringIO

from src import utils


def test_dependency_logger_cannot_emit_or_propagate_sensitive_exceptions() -> None:
    """Third-party solver tracebacks never bypass Byparr's redacted logger."""
    importlib.reload(utils)
    dependency_logger = logging.getLogger("playwright_captcha")
    parent_logger = logging.getLogger()
    stream = StringIO()
    parent_handler = logging.StreamHandler(stream)
    original_parent_handlers = parent_logger.handlers[:]
    try:
        parent_logger.handlers[:] = [parent_handler]
        dependency_logger.error(
            "solver leaked https://secret.example/token?cookie=private",
            exc_info=RuntimeError("private traceback value"),
        )
    finally:
        parent_logger.handlers[:] = original_parent_handlers

    assert dependency_logger.propagate is False
    assert dependency_logger.level > logging.CRITICAL
    assert dependency_logger.handlers == [utils.solver_log_sink]
    assert isinstance(utils.solver_log_sink, logging.NullHandler)
    assert stream.getvalue() == ""
