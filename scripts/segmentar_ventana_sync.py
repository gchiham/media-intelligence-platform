"""Segmenta con el LLM en **sincrono** las grabaciones ya transcritas de una
ventana de tiempo que todavia no tienen entrada en `segmentation_cache`.

Por que existe habiendo `scripts/segment_backlog_batch_openai.py`: la Batch API
cuesta la mitad pero su latencia es de horas y no esta acotada por abajo -- para
un informe del dia que hay que tener "ahorita" no sirve (medido el 2026-08-18:
23 chunks en batch no completaron ni uno en varios minutos, mientras que el
mismo chunk en sincrono tardo 46 s). Este camino paga el doble por tener el
resultado en minutos.

**Reparte la carga entre las dos cuentas de OpenAI** (`OPENAI_API_KEY` y
`OPENAI_API_KEY_2`) alternando por grabacion, con un pool de hilos: son dos
cuotas independientes, asi que el rate limit efectivo se duplica.

No hace clipping ni crea Noticia -- igual que el camino de batch, solo llena
`segmentation_cache`, que es de donde lee `scripts/informe_cliente.py`.

Uso:
    python scripts/segmentar_ventana_sync.py \\
        --desde 2026-08-18T10:00:00Z --hasta 2026-08-18T19:00:00Z \\
        --medios canal_11,canal_5,hch_tv
"""
import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from src.infrastructure.config import settings  # noqa: E402
from src.infrastructure.db import registry  # noqa: F401,E402
from src.infrastructure.db.engine import get_engine  # noqa: E402
from src.modules.ai.models import SegmentationCache  # noqa: E402
from src.modules.ai.providers.openai_provider import OpenAIAnalysisProvider  # noqa: E402
from src.modules.ai.schemas import Word  # noqa: E402


def _clave2() -> str | None:
    """La segunda cuenta no es campo de Settings (pydantic ignora las env vars
    que no declara), asi que se lee del entorno o del .env directo."""
    if os.environ.get("OPENAI_API_KEY_2"):
        return os.environ["OPENAI_API_KEY_2"]
    env = Path(__file__).parent.parent / ".env"
    if env.exists():
        for linea in env.read_text(encoding="utf-8").splitlines():
            if linea.strip().startswith("OPENAI_API_KEY_2="):
                return linea.split("=", 1)[1].strip()
    return None


def pendientes(desde: datetime, hasta: datetime, medios: list[str]) -> list[str]:
    """Grabaciones de la ventana que ya tienen transcripcion pero no cache."""
    filtro_medios = "AND m.codigo = ANY(:medios)" if medios else ""
    sql = text(f"""
        SELECT g.id::text
        FROM grabaciones g
        JOIN programas p ON p.id = g.programa_id
        JOIN medios m ON m.id = p.medio_id
        JOIN transcripciones t ON t.grabacion_id = g.id
        LEFT JOIN segmentation_cache sc ON sc.grabacion_id = g.id
        WHERE g.fecha_inicio >= :desde AND g.fecha_inicio < :hasta
          AND sc.grabacion_id IS NULL
          {filtro_medios}
        ORDER BY g.fecha_inicio
    """)
    with Session(get_engine()) as s:
        params = {"desde": desde, "hasta": hasta}
        if medios:
            params["medios"] = medios
        return [fila[0] for fila in s.execute(sql, params)]


def _segmentar_una(proveedor, gid: str, modelo: str):
    with Session(get_engine()) as s:
        fila = s.execute(
            text("SELECT segmentos FROM transcripciones WHERE grabacion_id = :g LIMIT 1"),
            {"g": gid},
        ).first()
        if not fila or not fila[0]:
            return gid, "sin transcripcion", None
        words = [Word.model_validate(w) for w in (fila[0] or {}).get("words", [])]
        if not words:
            return gid, "transcripcion vacia", None

        t0 = time.monotonic()
        try:
            segmentos = proveedor.segment_news(words)
        except Exception as exc:  # noqa: BLE001 -- una grabacion que falle no tumba el resto
            return gid, f"error: {exc}", None
        dt = time.monotonic() - t0

        # Puede haber corrido otro proceso en paralelo (el cron de batch, otra
        # sesion): la columna es unique, asi que se comprueba antes de insertar.
        ya = s.execute(
            text("SELECT 1 FROM segmentation_cache WHERE grabacion_id = :g"), {"g": gid}
        ).first()
        if ya:
            return gid, "ya estaba cacheada (otra corrida gano)", 0

        s.add(SegmentationCache(
            grabacion_id=gid,
            segmentos=[seg.model_dump(mode="json") for seg in segmentos],
            modelo=modelo,
            consumido=False,
        ))
        s.commit()
        return gid, f"ok ({len(segmentos)} noticias, {dt:.1f}s)", len(segmentos)


def _parse_utc(valor: str, flag: str) -> datetime:
    """Rechaza fechas sin zona horaria: un naive contra timestamptz se
    interpreta en la TZ de la sesion de Postgres, no en la del operador --
    la ventana consultada queda corrida 6 h sin ningun aviso (mismo criterio
    que informe_cliente.py)."""
    dt = datetime.fromisoformat(valor.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise SystemExit(
            f"{flag} {valor!r} no tiene zona horaria -- usa Z u offset explicito "
            f"(ej. {valor}Z para UTC, {valor}-06:00 para hora de Honduras)"
        )
    return dt


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--desde", required=True)
    p.add_argument("--hasta", required=True)
    p.add_argument("--medios", default="", help="codigos separados por coma; vacio = todos")
    p.add_argument("--concurrencia", type=int, default=10)
    a = p.parse_args()

    desde = _parse_utc(a.desde, "--desde")
    hasta = _parse_utc(a.hasta, "--hasta")
    medios = [x.strip() for x in a.medios.split(",") if x.strip()]

    gids = pendientes(desde, hasta, medios)
    if not gids:
        print("nada pendiente de segmentar en la ventana")
        return

    modelo = settings.openai_model
    clave1 = settings.openai_api_key.get_secret_value() if settings.openai_api_key else None
    if not clave1:
        raise SystemExit("falta OPENAI_API_KEY")
    clave2 = _clave2()

    proveedores = [OpenAIAnalysisProvider(api_key=clave1, model=modelo)]
    if clave2:
        proveedores.append(OpenAIAnalysisProvider(api_key=clave2, model=modelo))
    print(f"{len(gids)} grabaciones | modelo {modelo} | "
          f"{len(proveedores)} cuenta(s) OpenAI | concurrencia {a.concurrencia}")

    ok = total = 0
    errores = []
    with ThreadPoolExecutor(max_workers=a.concurrencia) as pool:
        futuros = {
            pool.submit(_segmentar_una, proveedores[i % len(proveedores)], gid, modelo): gid
            for i, gid in enumerate(gids)
        }
        for fut in as_completed(futuros):
            gid, msg, n = fut.result()
            print(f"  {gid}: {msg}")
            if n is None:
                errores.append(f"{gid}: {msg}")
            else:
                ok += 1
                total += n

    print(f"\n{ok}/{len(gids)} segmentadas, {total} noticias")
    if errores:
        print(f"{len(errores)} con error:")
        for e in errores:
            print(f"  {e}")


if __name__ == "__main__":
    main()
