"""Excepciones del dominio Pipeline -- mismo patron que
src/modules/editorial/exceptions.py."""


class PipelineDomainError(Exception):
    pass


class GrabacionNoEncontrada(PipelineDomainError):
    def __init__(self, grabacion_id):
        super().__init__(f"Grabacion {grabacion_id} no existe")
        self.grabacion_id = grabacion_id


class RecursosNoDisponibles(PipelineDomainError):
    """La Grabacion existe, pero los archivos que el pipeline necesita
    (words.json y/o audio) no estan disponibles todavia -- ej. chepita no ha
    terminado de transcribirla, o (en esta fase, sin integracion S3) no estan
    en el directorio local esperado."""

    def __init__(self, grabacion_id, detalle: str):
        super().__init__(f"Recursos no disponibles para grabacion {grabacion_id}: {detalle}")
        self.grabacion_id = grabacion_id
