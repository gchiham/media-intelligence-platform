"""Excepciones del dominio Editorial. Todas heredan de NoticiaDomainError para
que el llamador (futuro endpoint) pueda atraparlas con un solo except si
quiere, o discriminar por tipo si necesita un codigo HTTP distinto por caso."""


class NoticiaDomainError(Exception):
    pass


class NoticiaNoEncontrada(NoticiaDomainError):
    def __init__(self, noticia_id):
        super().__init__(f"Noticia {noticia_id} no existe")
        self.noticia_id = noticia_id


class ColaVacia(NoticiaDomainError):
    """No hay ninguna noticia PENDIENTE disponible para tomar (FR-050)."""


class NoticiaNoBloqueadaPorEditor(NoticiaDomainError):
    """La noticia no esta EN_REVISION, o esta asignada a otro editor -- viola
    el bloqueo exclusivo de FR-051."""

    def __init__(self, noticia_id, editor_id):
        super().__init__(
            f"Noticia {noticia_id} no esta bloqueada para el editor {editor_id} "
            f"(no esta EN_REVISION, o la tiene otro editor)"
        )
        self.noticia_id = noticia_id
        self.editor_id = editor_id


class TransicionEstadoInvalida(NoticiaDomainError):
    def __init__(self, noticia_id, estado_actual, accion):
        super().__init__(
            f"No se puede {accion} la noticia {noticia_id} en estado {estado_actual}"
        )
        self.noticia_id = noticia_id
        self.estado_actual = estado_actual
        self.accion = accion
