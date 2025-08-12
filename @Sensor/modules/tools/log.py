# modules/tools/log.py
import os, sys
import logging
from logging.handlers import RotatingFileHandler

MODULE_NAME = "SENSOR"

logger = logging.getLogger(MODULE_NAME)
logger.propagate = False

if not getattr(logger, "_is_configured", False):
    log_dir = os.getenv("LOG_DIR", "./logs")
    log_file = os.getenv(MODULE_NAME + "_LOG_FILE", "sensor.log")
    log_level = os.getenv(MODULE_NAME + "_LOG_LEVEL", "INFO").upper()
    max_bytes = int(os.getenv("LOG_MAX_BYTES", 5 * 1024 * 1024))
    backup_count = int(os.getenv("LOG_BACKUP_COUNT", 5))

    os.makedirs(log_dir, exist_ok=True)

    base, ext = os.path.splitext(log_file)
    if not ext:
        ext = ".log"

    # UUID: prima SENSOR_UUID, poi MODULE_NAME_UUID, infine "default"
    uuid_suffix = (os.getenv("SENSOR_UUID")
                   or os.getenv(MODULE_NAME + "_UUID")
                   or "default")

    log_path = os.path.join(log_dir, f"{base}_{uuid_suffix}{ext}")

    level = getattr(logging, log_level, None)
    if not isinstance(level, int):
        raise ValueError(f"[LOG] Livello di log '{log_level}' non valido.")
    logger.setLevel(level)

    formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s",
                                  "%Y-%m-%d %H:%M:%S")

    file_handler = RotatingFileHandler(log_path, maxBytes=max_bytes,
                                       backupCount=backup_count, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)  # stdout compatibile con servizi
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    if os.getenv("DEBUG_LOG_INIT", "0") == "1":
        print(f"[LOG] Logger configurato su {log_path} livello={log_level}")

    logger._is_configured = True

def log(msg: str, level: str = "info", section: str = MODULE_NAME, exc_info=False):
    prefix = f"[{section}]" if section else ""
    full_msg = f"{prefix} {msg}".strip()
    level = level.lower()
    log_func = {
        "debug": logger.debug,
        "warning": logger.warning,
        "error": logger.error,
        "critical": logger.critical,
    }.get(level, logger.info)
    log_func(full_msg, exc_info=exc_info)
