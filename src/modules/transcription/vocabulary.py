"""Vocabulario de terminos hondureños para sesgar la transcripcion hacia los
nombres propios que importan en monitoreo de medios.

Por que existe: Whisper transcribe mal nombres propios de forma consistente --
en español el problema se agrava porque combinan acentos con secuencias de
letras poco frecuentes. Y en un producto de monitoreo el nombre propio ES el
producto: si el sistema escribe "Siomara Castro" en vez de "Xiomara Castro",
la busqueda por entidad del cliente simplemente no encuentra la mencion.

**Limite duro que condiciona el diseño de este modulo:** el mecanismo de
sesgo de Whisper (`hotwords`/`initial_prompt`) se inyecta en la ventana de
condicionamiento del decoder, que son ~224 tokens. No es una lista de
vocabulario ilimitada -- pasarle cientos de terminos no solo se trunca, sino
que puede *degradar* la transcripcion al llenar esa ventana con ruido. Por eso
`build_hotwords()` corta en `max_terms` y por eso la semilla de abajo es corta
y curada a proposito, no un volcado del catalogo completo de `Entidad`.

Criterio para agregar un termino aca: que sea (a) frecuente en el aire
hondureño, y (b) que Whisper lo escriba mal sin ayuda. Un termino que el
modelo ya acierta no gana nada y ocupa presupuesto de tokens.
"""

# Semilla curada. NO crece indefinidamente -- ver la nota de 224 tokens arriba.
# Para menciones de cola larga, el camino correcto es la resolucion de
# entidades contra el catalogo `Entidad` (post-transcripcion), no engordar esto.
TERMINOS_SEMILLA: list[str] = [
    # Moneda y unidades que aparecen en casi toda nota economica
    "lempiras",
    # Geografia: departamentos y ciudades que Whisper suele castellanizar mal
    "Tegucigalpa",
    "Comayaguela",
    "San Pedro Sula",
    "Choluteca",
    "Comayagua",
    "Intibuca",
    "Ocotepeque",
    "Copan",
    "Olancho",
    "Gracias a Dios",
    "Islas de la Bahia",
    "Roatan",
    "Tela",
    "Danli",
    "Siguatepeque",
    "Juticalpa",
    "Catacamas",
    # Instituciones publicas (siglas + nombre, ambas formas salen al aire)
    "ENEE",
    "SANAA",
    "IHSS",
    "COPECO",
    "UNAH",
    "Hondutel",
    "CNE",
    "Consejo Nacional Electoral",
    "Congreso Nacional",
    "Corte Suprema de Justicia",
    "Ministerio Publico",
    "Fiscalia General",
    "Secretaria de Salud",
    "Policia Nacional",
    "Fuerzas Armadas",
    # Partidos y movimientos
    "Partido Nacional",
    "Partido Liberal",
    "Libertad y Refundacion",
    "LIBRE",
    # Medios propios del monitoreo (se nombran entre si al aire)
    "Televicentro",
    "Radio America",
    "Radio Globo",
    "HCH",
]


def build_hotwords(terminos: list[str] | None = None, max_terms: int = 60) -> str:
    """Arma el string de sesgo que se le pasa a Whisper.

    `max_terms` no es un numero arbitrario: es el freno para no desbordar la
    ventana de ~224 tokens del decoder (ver docstring del modulo). Si se
    necesita priorizar, pasar `terminos` ya ordenado por frecuencia real de
    aparicion -- el corte es por el principio de la lista.
    """
    seleccion = (terminos if terminos is not None else TERMINOS_SEMILLA)[:max_terms]
    return ", ".join(seleccion)
