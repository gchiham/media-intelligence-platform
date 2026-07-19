"""Repository generico (patron Repository) -- adapter de infraestructura que
envuelve acceso a datos via SQLAlchemy detras de una interfaz simple y comun a
todos los modulos. Los repositorios de cada modulo (NoticiaRepository,
PipelineRunRepository, etc.) heredan de esta base y solo declaran `model`;
consultas especificas de negocio (filtros, joins) se agregan en la siguiente
fase -- esto es solo la estructura, sin logica de negocio todavia.
"""
import uuid
from typing import Generic, TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.infrastructure.db.base import Base

ModelT = TypeVar("ModelT", bound=Base)


class Repository(Generic[ModelT]):
    model: type[ModelT]

    def __init__(self, session: Session):
        self._session = session

    def get_by_id(self, id: uuid.UUID) -> ModelT | None:
        return self._session.get(self.model, id)

    def list(self, limit: int = 100, offset: int = 0) -> list[ModelT]:
        stmt = select(self.model).limit(limit).offset(offset)
        return list(self._session.scalars(stmt))

    def add(self, instance: ModelT) -> ModelT:
        self._session.add(instance)
        return instance

    def commit(self) -> None:
        self._session.commit()
