"""Politica de backoff para reintentos de mensajes SQS.

La cola real `media-intel-transcription-jobs` ya tiene un RedrivePolicy
configurado en AWS con `maxReceiveCount=3` -- eso es el limite duro y el
respaldo final (si algo se clasifica mal como transitorio, a la 4ta entrega
SQS lo manda solo a la DLQ, sin que nuestro codigo tenga que hacer nada). Este
modulo solo controla CUANTO esperar entre reintento y reintento dentro de esos
3 intentos, via `ChangeMessageVisibility` -- sin esto, el espaciado entre
reintentos seria siempre el VisibilityTimeout fijo de la cola (1800s), que es
demasiado lento para un rate limit o un blip de red de unos segundos.
"""

# Backoff creciente por numero de intento (1-indexado, coincide con
# ApproximateReceiveCount de SQS). Con maxReceiveCount=3, el intento 4 nunca
# llega a nuestro codigo -- SQS lo redirige a la DLQ antes de entregarlo.
_BACKOFF_SCHEDULE_SECONDS = [30, 90, 240]
_MAX_BACKOFF_SECONDS = 300


def compute_backoff_seconds(attempt: int) -> int:
    """`attempt` = ApproximateReceiveCount del mensaje (1 en el primer intento)."""
    if attempt <= 0:
        raise ValueError(f"attempt debe ser >= 1, recibido {attempt}")
    idx = attempt - 1
    if idx < len(_BACKOFF_SCHEDULE_SECONDS):
        return _BACKOFF_SCHEDULE_SECONDS[idx]
    return _MAX_BACKOFF_SECONDS
