"""Identificacion de medio y fecha desde el nombre del archivo.

Se resuelve con regex y no preguntandole al modelo: sobre los 70 PDF reales de
agosto 2026 acierta el medio en 70 y la fecha en 69 -- el unico que falla no
trae el dia en el nombre. Un LLM ahi solo agregaria costo y riesgo de
alucinacion en un dato determinista.

Los nombres de abajo son los reales del canal, con sus rarezas: el prefijo del
id de Telegram, la "a_" que quedo al transliterar "Mas", y los sufijos de
duplicado que agrega el canal al republicar.
"""
from datetime import date

import pytest

from src.modules.prensa.pdf import identificar


@pytest.mark.parametrize(
    "nombre, medio, fecha",
    [
        ("12611_DiarioTiempo-13-08-26_compressed.pdf", "diario_tiempo", date(2026, 8, 13)),
        ("12645_LA-TRIBUNA-PDF-WEB-ZDT-26082026.pdf", "la_tribuna", date(2026, 8, 26)),
        ("12609_Jueves_13_de_Agosto_de_2026_Ma_s_Noticias.pdf", "mas_noticias", date(2026, 8, 13)),
        ("12653_EP_Agosto_280826_ZDR1.pdf", "el_pais_hn", date(2026, 8, 28)),
        ("12649_EP_Pais_270826_NT5P.pdf", "el_pais_hn", date(2026, 8, 27)),
        (
            "12622_SABADO_15_DE_AGOSTO_2026_-_LA_PATRULLA_GRAFICA_copia.pdf",
            "patrulla_grafica",
            date(2026, 8, 15),
        ),
        # Republicado por el canal con sufijo: mismo medio y misma fecha.
        ("12573_DiarioTiempo-01-08-26_compressed-1.pdf", "diario_tiempo", date(2026, 8, 1)),
        ("12651_LA-TRIBUNA-PDF-WEB-HNN-27082026_1_.pdf", "la_tribuna", date(2026, 8, 27)),
    ],
)
def test_identifica_medio_y_fecha(nombre, medio, fecha):
    ident = identificar(nombre)
    assert ident.medio == medio
    assert ident.fecha == fecha


def test_mes_no_se_come_el_guion_bajo():
    """`\\w` incluye "_" en Python, asi que capturaba "agosto_" y no matcheaba
    contra el diccionario de meses. Es el bug que dejaba sin fecha a todo Mas
    Noticias, que separa cada palabra con guion bajo."""
    assert identificar("Lunes_10_de_Agosto_de_2026_Ma_s_Noticias.pdf").fecha == date(2026, 8, 10)


def test_acepta_fecha_sin_el_segundo_de():
    """La Patrulla Grafica escribe "15 DE AGOSTO 2026", sin "de" antes del anio."""
    assert identificar("SABADO_15_DE_AGOSTO_2026_-_LA_PATRULLA_GRAFICA.pdf").fecha == date(
        2026, 8, 15
    )


def test_sin_dia_en_el_nombre_devuelve_none_en_vez_de_adivinar():
    """Caso real: "MIERCOLES DE AGOSTO 2026" no dice que dia es. Preferimos
    None (y que alguien lo revise) antes que inventar una fecha."""
    ident = identificar("12592_MIERCOLES-DE-AGOSTO-2026-LA-PATRULLA-GRAFICA-copia.pdf")
    assert ident.medio == "patrulla_grafica"
    assert ident.fecha is None


def test_medio_desconocido_no_revienta():
    ident = identificar("99999_El_Diario_Que_No_Conocemos_01012026.pdf")
    assert ident.medio is None
