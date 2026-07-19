"""Logging estructurado en JSON, usando el modulo `logging` estandar (sin
dependencias nuevas) -- resuelve la deuda tecnica R7 de ARCHITECTURE_REVIEW.md
para el camino de errores del pipeline."""
import json
import logging
import sys

from src.shared.error_context import ErrorContext


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(extra)
        return json.dumps(payload, ensure_ascii=False)


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def log_pipeline_error(logger: logging.Logger, ctx: ErrorContext) -> None:
    """Un solo log estructurado por error, con exactamente los campos pedidos:
    job_id, audio original, error completo, stack trace, fecha, numero de
    intento, modulo que fallo."""
    logger.error(
        f"pipeline error en modulo={ctx.module} job_id={ctx.job_id} attempt={ctx.attempt}",
        extra={"extra_fields": ctx.model_dump()},
    )
