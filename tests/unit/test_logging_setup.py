import logging
from pathlib import Path

from vnmaster.logging_setup import configure_logging, get_logger


def test_configure_logging_writes_to_file(tmp_path: Path) -> None:
    log_file = tmp_path / "vnmaster.log"
    configure_logging(log_file=log_file, level=logging.DEBUG)
    log = get_logger("vnmaster.test")
    log.info("hello world")
    for handler in logging.getLogger().handlers:
        handler.flush()
    contents = log_file.read_text()
    assert "hello world" in contents
    assert "vnmaster.test" in contents


def test_get_logger_returns_module_logger() -> None:
    log = get_logger("vnmaster.foo")
    assert log.name == "vnmaster.foo"
