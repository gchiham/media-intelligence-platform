"""Armado del payload del dashboard de pipeline y el candado por token.

Sin base de datos: `resumir()` es pura y las rutas se prueban por el lado que
rechaza (404), que no llega a tocar la sesion.
"""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from src.api.main import app
from src.infrastructure.config import settings
from src.modules.pipeline.metricas import inicio_ventana, resumir

AHORA = datetime(2026, 8, 26, 15, 50, tzinfo=timezone.utc)


def _hora(h: int) -> datetime:
    return datetime(2026, 8, 26, h, tzinfo=timezone.utc)


def _fila_hora(h: int, grab: int, trans: int, seg: int, clip: int, **extra) -> dict:
    fila = {
        "hora": _hora(h),
        "grabaciones": grab,
        "transcritas": trans,
        "segmentadas": seg,
        "clipeadas": clip,
        "errores": 0,
        "noticias": 0,
        "clips": 0,
    }
    fila.update(extra)
    return fila


def _fila_medio(codigo: str, ultima: int, hasta_etapa: str = "clipeadas", **extra) -> dict:
    """Un medio cuyas cuatro etapas llegan a la hora `ultima`, salvo las
    posteriores a `hasta_etapa`, que quedan en None."""
    orden = ["grabaciones", "transcritas", "segmentadas", "clipeadas"]
    corte = orden.index(hasta_etapa)
    ts = [_hora(ultima) if i <= corte else None for i in range(4)]
    fila = {
        "codigo": codigo,
        "nombre": codigo.upper(),
        "tipo": "radio",
        "ultima_grabacion": ts[0],
        "ultima_transcrita": ts[1],
        "ultima_segmentada": ts[2],
        "ultima_clipeada": ts[3],
        "grabaciones": 4,
        "transcritas": 4,
        "segmentadas": 4,
        "clipeadas": 4,
    }
    fila.update(extra)
    return fila


def test_embudo_cuenta_pendientes_contra_la_etapa_anterior():
    """Lo que le falta a cada etapa se mide contra su unica entrada -- la etapa
    de arriba -- no contra el total de grabaciones."""
    d = resumir(
        por_hora=[_fila_hora(12, 10, 10, 8, 5), _fila_hora(13, 10, 9, 4, 0)],
        por_medio=[_fila_medio("radio_globo", 13)],
        batches=[],
        horas=24,
        ahora=AHORA,
    )
    etapas = {e["clave"]: e for e in d["etapas"]}
    assert etapas["grabaciones"]["total"] == 20 and etapas["grabaciones"]["pendientes"] == 0
    assert etapas["transcritas"]["total"] == 19 and etapas["transcritas"]["pendientes"] == 1
    assert etapas["segmentadas"]["total"] == 12 and etapas["segmentadas"]["pendientes"] == 7
    assert etapas["clipeadas"]["total"] == 5 and etapas["clipeadas"]["pendientes"] == 7
    assert etapas["clipeadas"]["pct"] == 25.0


def test_ventana_vacia_no_divide_entre_cero():
    d = resumir(por_hora=[], por_medio=[], batches=[], horas=24, ahora=AHORA)
    assert all(e["total"] == 0 and e["pct"] == 0.0 for e in d["etapas"])
    assert d["alertas"] == []


def test_medio_atrasado_se_mide_contra_el_mas_adelantado_no_contra_el_reloj():
    """A las 15:50 UTC lo normal es que la ultima hora cerrada sea la de las
    14:00; eso no es atraso. Quedarse en las 09:00 con los demas en 14:00 si."""
    d = resumir(
        por_hora=[_fila_hora(14, 2, 2, 2, 2)],
        por_medio=[_fila_medio("radio_globo", 14), _fila_medio("canal_5", 9)],
        batches=[],
        horas=24,
        ahora=AHORA,
    )
    medios = {m["codigo"]: m for m in d["por_medio"]}
    assert medios["radio_globo"]["atrasado"] is False
    assert medios["canal_5"]["atrasado"] is True
    # Las cuatro etapas de canal_5 llegan igual de lejos: no hay nada trabado en
    # el pipeline, lo que falto fue la captura aguas arriba.
    assert medios["canal_5"]["sin_captura"] is True
    assert "canal_5" in d["alertas"][0]["texto"]


def test_medio_atrasado_con_etapas_desparejas_no_es_falta_de_captura():
    d = resumir(
        por_hora=[_fila_hora(14, 2, 2, 2, 2)],
        por_medio=[
            _fila_medio("radio_globo", 14),
            _fila_medio("xy_hrn", 9, hasta_etapa="transcritas"),
        ],
        batches=[],
        horas=24,
        ahora=AHORA,
    )
    medios = {m["codigo"]: m for m in d["por_medio"]}
    assert medios["xy_hrn"]["atrasado"] is True
    assert medios["xy_hrn"]["sin_captura"] is False
    assert medios["xy_hrn"]["ultima_segmentada"] is None


def test_grabaciones_en_error_generan_alerta():
    d = resumir(
        por_hora=[_fila_hora(14, 5, 4, 4, 4, errores=1)],
        por_medio=[_fila_medio("radio_globo", 14)],
        batches=[],
        horas=24,
        ahora=AHORA,
    )
    assert any(a["nivel"] == "danger" and "error" in a["texto"] for a in d["alertas"])


def test_batch_de_openai_estancado_genera_alerta_y_uno_reciente_no():
    batches = [
        {"cuenta": "1", "batches": 2, "requests": 40, "mas_viejo": AHORA - timedelta(hours=3)},
        {"cuenta": "2", "batches": 1, "requests": 20, "mas_viejo": AHORA - timedelta(minutes=20)},
    ]
    d = resumir(
        por_hora=[_fila_hora(14, 5, 5, 5, 5)],
        por_medio=[_fila_medio("radio_globo", 14)],
        batches=batches,
        horas=24,
        ahora=AHORA,
    )
    textos = [a["texto"] for a in d["alertas"]]
    assert any("Cuenta 1" in t for t in textos)
    assert not any("Cuenta 2" in t for t in textos)


def test_fechas_salen_en_iso_utc():
    d = resumir(
        por_hora=[_fila_hora(14, 1, 1, 1, 1)],
        por_medio=[_fila_medio("radio_globo", 14)],
        batches=[],
        horas=24,
        ahora=AHORA,
    )
    assert d["por_hora"][0]["hora"] == "2026-08-26T14:00:00+00:00"
    assert d["etapas"][0]["ultima_hora"] == "2026-08-26T14:00:00+00:00"
    assert d["generado_at"] == "2026-08-26T15:50:00+00:00"


def test_inicio_de_ventana_se_alinea_a_la_hora_en_punto():
    assert inicio_ventana(24, ahora=AHORA) == datetime(2026, 8, 25, 15, tzinfo=timezone.utc)


def test_sin_token_la_pagina_y_la_api_responden_404(monkeypatch):
    monkeypatch.setattr(settings, "dashboard_token", "secreto")
    client = TestClient(app)
    assert client.get("/dash").status_code == 404
    assert client.get("/dash?token=otro").status_code == 404
    assert client.get("/api/v1/dash/metricas?token=otro").status_code == 404


def test_con_token_la_pagina_se_sirve(monkeypatch):
    monkeypatch.setattr(settings, "dashboard_token", "secreto")
    respuesta = TestClient(app).get("/dash?token=secreto")
    assert respuesta.status_code == 200
    # El token no se incrusta en el HTML: lo lee el JS de la query string.
    assert "secreto" not in respuesta.text
    assert "Avance del pipeline" in respuesta.text
