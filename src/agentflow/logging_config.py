"""Centralized logging configuration for the application."""

import logging
import sys


def setup_logging(level: str = "INFO", json_format: bool = False) -> None:
    """
    Configure application-wide logging.

    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        json_format: Whether to output logs in JSON format (useful for production)
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    # Create formatters
    if json_format:
        # For production - structured logging
        formatter = logging.Formatter(
            '{"time":"%(asctime)s","level":"%(levelname)s","name":"%(name)s",'
            '"message":"%(message)s","module":"%(module)s","function":"%(funcName)s"}'
        )
    else:
        # For development - human-readable
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)8s] [%(name)s] %(message)s "
            "(%(filename)s:%(lineno)d)",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # Remove existing handlers
    root_logger.handlers.clear()

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # Reduce noise from third-party libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("yfinance").setLevel(logging.WARNING)

    logging.info(f"Logging configured at {level} level")


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger instance for a module.

    Args:
        name: Logger name (typically __name__)

    Returns:
        Configured logger instance
    """
    return logging.getLogger(name)
