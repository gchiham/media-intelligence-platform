"""Empalma menciones frescas dentro del bundle de Kronos Signal.

Corre en el HOST (no necesita DB). El bundle embebe sus datos como
x=JSON.parse(`[...]`); este script localiza ese template literal, lo reemplaza
por el JSON nuevo, y bumpea ?v= en index.html para reventar el cache del
navegador (el nombre del asset no cambia).

Uso: python3 parchar_bundle.py /ruta/menciones.json
"""
import json
import re
import shutil
import sys
import time
from pathlib import Path

DIST = Path("/home/ubuntu/kronos-signal/dist")
BUNDLE = DIST / "assets" / "index-BUiZHVla.js"
BACKUP = Path("/home/ubuntu/kronos-signal/index-BUiZHVla.js.orig")
MARCADOR = 'JSON.parse(`[{"id":'


def escapar_para_template(js: str) -> str:
    # Orden importa: primero backslash, despues backtick y ${.
    return js.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")


def main() -> None:
    datos = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    if not isinstance(datos, list) or len(datos) < 10:
        raise SystemExit(f"abortado: solo {len(datos)} menciones -- no piso el bundle con eso")

    if not BACKUP.exists():
        shutil.copy2(BUNDLE, BACKUP)

    s = BUNDLE.read_text(encoding="utf-8")
    i = s.find(MARCADOR)
    if i < 0:
        # ya parchado antes? el marcador generico: JSON.parse(` seguido de [{"
        m = re.search(r"JSON\.parse\(`\[\{", s)
        if not m:
            raise SystemExit("no encontre el array de menciones en el bundle")
        i = m.start()
    ini = s.index("`", i) + 1
    # fin: el primer backtick no escapado despues de ini
    j = ini
    while True:
        j = s.index("`", j)
        k = j - 1
        barras = 0
        while s[k] == "\\":
            barras += 1
            k -= 1
        if barras % 2 == 0:
            break
        j += 1
    nuevo = escapar_para_template(json.dumps(datos, ensure_ascii=False, separators=(",", ":")))
    s = s[:ini] + nuevo + s[j:]
    BUNDLE.write_text(s, encoding="utf-8")

    # cache-bust en index.html
    idx = DIST / "index.html"
    h = idx.read_text(encoding="utf-8")
    v = int(time.time())
    h = re.sub(r"(index-[A-Za-z0-9_-]+\.js)(\?v=\d+)?", rf"\1?v={v}", h)
    h = re.sub(r"(index-[A-Za-z0-9_-]+\.css)(\?v=\d+)?", rf"\1?v={v}", h)
    idx.write_text(h, encoding="utf-8")
    print(f"bundle parchado con {len(datos)} menciones, cache-bust v={v}")


if __name__ == "__main__":
    main()
