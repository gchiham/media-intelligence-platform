"""Empalma menciones frescas dentro del bundle de Kronos Signal.

Corre en el HOST (no necesita DB). El bundle embebe sus datos como
x=JSON.parse(`[...]`); este script localiza ese template literal, lo reemplaza
por el JSON nuevo, y bumpea ?v= en index.html para reventar el cache del
navegador (el nombre del asset no cambia entre parches).

Uso: python3 parchar_bundle.py /ruta/menciones.json

El bundle NO se busca por nombre fijo: se lee de index.html. El 24-ago se
desplego un build nuevo (index-BUiZHVla.js -> index-D1FeIfGh.js) y el parche,
que tenia el hash viejo hardcodeado, murio en cada corrida por 16 horas sin
que nadie lo notara -- la pagina quedo mostrando los datos congelados del
build (radio y TV al 8-ago, o sea nada dentro de la ventana de 24 h que filtra
la UI).

Del build solo se conservan los items que nosotros NO producimos (social y
prensa impresa). Si se copiaran tambien los de tv/radio/prensa_rss, los
duplicados del demo se mezclarian con los reales.
"""
import json
import re
import shutil
import sys
import time
from pathlib import Path

RAIZ = Path("/home/ubuntu/kronos-signal")
DIST = RAIZ / "dist"
MARCADOR = re.compile(r"JSON\.parse\(`\[\{\"id\"")
# Los tipos que salen de nuestra DB (ver generar_menciones.py). El resto
# ("social", "prensa_impresa") solo existe en el build y se hereda tal cual.
TIPOS_NUESTROS = {"tv", "radio", "prensa_rss"}

ESCAPES_JS = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}


def bundle_actual() -> Path:
    """El asset que index.html esta cargando hoy."""
    html = (DIST / "index.html").read_text(encoding="utf-8")
    m = re.search(r"assets/(index-[A-Za-z0-9_-]+\.js)", html)
    if not m:
        raise SystemExit("index.html no referencia ningun assets/index-*.js")
    return DIST / "assets" / m.group(1)


def _desescapar_js(texto: str) -> str:
    """Deshace los escapes del template literal para recuperar el JSON.

    No alcanza con quitar \\` y \\$: el minificador tambien emite \\xA0 y
    \\uXXXX, que JSON no entiende (json.loads truena con "Invalid \\escape").
    """
    salida = []
    i = 0
    while i < len(texto):
        c = texto[i]
        if c != "\\" or i + 1 >= len(texto):
            salida.append(c)
            i += 1
            continue
        sig = texto[i + 1]
        if sig == "x":
            salida.append(chr(int(texto[i + 2:i + 4], 16)))
            i += 4
        elif sig == "u":
            salida.append(chr(int(texto[i + 2:i + 6], 16)))
            i += 6
        elif sig in ESCAPES_JS:
            # Ojo: un \\n dentro del JSON llega aca como \\\\n y ya se resolvio
            # en la rama de abajo (\\ -> \), asi que esto solo pega en escapes
            # de nivel template.
            salida.append(ESCAPES_JS[sig])
            i += 2
        else:
            salida.append(sig)
            i += 2
    return "".join(salida)


def escapar_para_template(js: str) -> str:
    # Orden importa: primero backslash, despues backtick y ${.
    return js.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")


def limites_del_arreglo(s: str) -> tuple[int, int]:
    """(inicio, fin) del contenido del template literal de menciones."""
    m = MARCADOR.search(s)
    if not m:
        raise SystemExit("no encontre el arreglo de menciones en el bundle")
    ini = s.index("`", m.start()) + 1
    j = ini
    while True:
        j = s.index("`", j)
        k = j - 1
        barras = 0
        while s[k] == "\\":
            barras += 1
            k -= 1
        if barras % 2 == 0:
            return ini, j
        j += 1


def menciones_del_build(texto_original: str) -> list:
    ini, fin = limites_del_arreglo(texto_original)
    return json.loads(_desescapar_js(texto_original[ini:fin]))


def main() -> None:
    datos = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    if not isinstance(datos, list) or len(datos) < 10:
        raise SystemExit(f"abortado: solo {len(datos)} menciones -- no piso el bundle con eso")

    bundle = bundle_actual()
    backup = RAIZ / f"{bundle.name}.orig"
    if not backup.exists():
        shutil.copy2(bundle, backup)

    # Se parte SIEMPRE del build original, no del bundle ya parchado: asi el
    # resultado no depende de cuantas veces corrio esto antes.
    original = backup.read_text(encoding="utf-8")
    heredadas = [m for m in menciones_del_build(original) if m.get("tipo") not in TIPOS_NUESTROS]

    ini, fin = limites_del_arreglo(original)
    nuevo = escapar_para_template(
        json.dumps(datos + heredadas, ensure_ascii=False, separators=(",", ":"))
    )
    bundle.write_text(original[:ini] + nuevo + original[fin:], encoding="utf-8")

    # cache-bust en index.html
    idx = DIST / "index.html"
    h = idx.read_text(encoding="utf-8")
    v = int(time.time())
    h = re.sub(r"(index-[A-Za-z0-9_-]+\.js)(\?v=\d+)?", rf"\1?v={v}", h)
    h = re.sub(r"(index-[A-Za-z0-9_-]+\.css)(\?v=\d+)?", rf"\1?v={v}", h)
    idx.write_text(h, encoding="utf-8")
    print(
        f"{bundle.name} parchado con {len(datos)} menciones nuestras "
        f"+ {len(heredadas)} heredadas del build, cache-bust v={v}"
    )


if __name__ == "__main__":
    main()
