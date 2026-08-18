"""Filtra `transcripciones_3al11.txt` (noticias ya clippeadas, una por bloque)
para quedarse solo con las relevantes a JOH / Aspra / PGR / Guias de Familia,
usando la Batch API de OpenAI con gpt-5-mini.

El archivo de entrada ya viene segmentado en bloques delimitados por lineas
"====...====", cada uno con "FECHA | MEDIO | TITULO" y luego el texto. Este
script agrupa esos bloques en chunks (por presupuesto de caracteres) para no
pasarse del contexto, manda un chat completion por chunk con el prompt de
filtrado dado por el usuario, y concatena lo que el modelo devuelva.

No resume ni reescribe nada -- el prompt en si mismo instruye al modelo a
conservar la transcripcion original tal cual. Este script solo particiona,
envia, y junta.

Uso:
    python scripts/filtro_joh_batch_openai.py --submit
    python scripts/filtro_joh_batch_openai.py --seguir
    python scripts/filtro_joh_batch_openai.py --collect
"""
import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from openai import OpenAI  # noqa: E402

from src.infrastructure.config import settings  # noqa: E402

ENTRADA = Path(r"C:\Users\Sedesol\Desktop\transcripciones_3al11.txt")
SALIDA = Path("data/filtro_joh_semana")
ESTADO = SALIDA / "estado.json"
PERIODO = "3 al 11 de agosto de 2026"
MODELO_OPENAI = "gpt-5-mini"
PRESUPUESTO_CHARS = 150_000  # por chunk, generoso frente al contexto del modelo


def _leer_env_var(nombre: str) -> str | None:
    """OPENAI_API_KEY_2 no es parte de Settings (pydantic ignora env vars
    extra), asi que se lee directo del .env para la segunda cuenta."""
    env_path = Path(__file__).parent.parent / ".env"
    for linea in env_path.read_text(encoding="utf-8").splitlines():
        if linea.strip().startswith(f"{nombre}="):
            return linea.split("=", 1)[1].strip()
    return None

SYSTEM_PROMPT = """FILTRO DE NOTICIAS RELEVANTES PARA INFORME ESTRATÉGICO
ROL
Actúa como un analista senior de inteligencia mediática y monitoreo político.
Voy a proporcionarte un archivo que contiene múltiples noticias y transcripciones correspondientes a diferentes medios y programas durante una semana.
Tu tarea NO es elaborar el informe estratégico todavía.
Tu única tarea en esta etapa es identificar y extraer las noticias que realmente sean relevantes para un posterior informe estratégico sobre Juan Orlando Hernández.
TEMAS QUE NOS INTERESAN
Una noticia debe considerarse relevante si contiene información significativa sobre uno o varios de estos temas:
1. Juan Orlando Hernández
Incluye noticias relacionadas con:

* Juan Orlando Hernández;
* su proceso judicial;
* audiencia;
* declaraciones sobre JOH;
* declaraciones de JOH;
* defensa de JOH;
* acusaciones contra JOH;
* críticas o respaldo hacia JOH;
* situación política o reputacional de JOH;
* referencias a su administración cuando tengan relación con las narrativas actuales;
* actores que estén hablando públicamente sobre JOH.

2. Dagoberto Aspra
Incluye noticias relacionadas con:

* Dagoberto Aspra;
* declaraciones de Aspra;
* declaraciones sobre Aspra;
* acciones de Aspra;
* conflictos o intercambios públicos relacionados con JOH;
* críticas o respaldo hacia Aspra;
* cualquier actuación de Aspra que pueda tener relevancia política o mediática para JOH.

3. Procuraduría General de la República — PGR
Incluye noticias relacionadas con:

* Procuraduría General de la República;
* acciones de la PGR;
* declaraciones de la PGR;
* posición de la PGR respecto a JOH;
* actuaciones de la PGR relacionadas con el proceso;
* críticas o cuestionamientos a la PGR;
* presión política sobre la PGR;
* conflictos institucionales relacionados con la PGR.

4. Guías de Familia / Vida Mejor
Incluye noticias relacionadas con:

* Guías de Familia;
* Vida Mejor;
* demandas relacionadas con las Guías de Familia;
* procesos judiciales relacionados;
* reclamos relacionados con estos programas;
* actuaciones de la PGR relacionadas con este tema;
* presión política sobre la PGR derivada de este tema;
* cualquier conexión explícita con Juan Orlando Hernández o su administración.

CRITERIO DE RELEVANCIA
No incluyas una noticia simplemente porque mencione de pasada alguno de los nombres.
Debe existir contenido sustancial o potencialmente relevante.
Clasifica cada noticia como:
RELEVANCIA CRÍTICA
La noticia podría afectar directamente la percepción, estrategia o preparación de JOH ante su situación actual.
RELEVANCIA ALTA
La noticia contiene información importante sobre JOH, Aspra, PGR o Guías de Familia y puede contribuir significativamente al análisis posterior.
RELEVANCIA MEDIA
La noticia tiene relación con los temas, pero su impacto estratégico es limitado.
NO RELEVANTE
La mención es incidental, irrelevante o no aporta información útil para el análisis.
Solo debes incluir en el archivo final las noticias con relevancia CRÍTICA, ALTA o MEDIA.
Sin embargo, si hay demasiadas noticias de relevancia media y poco contenido sustancial, prioriza las de mayor relevancia.
REGLA IMPORTANTE: NO RESUMIR
Cuando encuentres una noticia relevante:
NO la resumas.
Extrae y conserva la transcripción completa de esa noticia, incluyendo:

* titular, si existe;
* fecha, si existe;
* medio;
* programa, si existe;
* periodista o presentador, si aparece;
* introducción;
* declaraciones;
* comentarios del periodista;
* entrevistas;
* contexto proporcionado por el medio;
* conclusión.

La finalidad es que otro modelo pueda analizar posteriormente el lenguaje y contexto original de la noticia.
NO MODIFICAR EL CONTENIDO
No:

* corrijas declaraciones;
* cambies palabras;
* interpretes frases;
* agregues contexto externo;
* completes información faltante;
* corrijas nombres basándote en conocimiento externo;
* inventes fechas;
* inventes titulares.

Conserva el texto original.
Puedes eliminar únicamente contenido claramente irrelevante que forme parte de la misma transcripción, por ejemplo:

* comerciales;
* publicidad;
* saludos;
* música;
* segmentos completamente ajenos;
* conversaciones que no tengan relación con la noticia.

Si existe duda sobre si una parte es relevante, consérvala.
NOTICIAS REPETIDAS
Es posible que la misma noticia aparezca en diferentes medios.
NO elimines automáticamente las repeticiones.
Cada versión puede ser importante porque:

* puede tener diferente lenguaje;
* puede presentar diferente tono;
* puede incluir declaraciones diferentes;
* puede mostrar cómo distintos medios están tratando el mismo tema.

Si son prácticamente idénticas, puedes marcar:
[MISMA NOTICIA / REPETIDA]
pero conserva la información necesaria para identificar cada medio.
SEPARAR NOTICIAS
Si una misma transcripción contiene varias noticias diferentes, sepáralas.
Por ejemplo:
NOTICIA 1 — JOH
NOTICIA 2 — PGR
NOTICIA 3 — Guías de Familia
No trates toda una transmisión como una sola noticia.
INFORMACIÓN QUE DEBES CONSERVAR
Para cada noticia extraída, utiliza este encabezado:
==================================================
NOTICIA [NÚMERO]
FECHA:
[si aparece]
MEDIO:
[si aparece]
PROGRAMA:
[si aparece]
PERIODISTA / PRESENTADOR:
[si aparece]
TEMA PRINCIPAL:
[JOH / Aspra / PGR / Guías de Familia / combinación]
RELEVANCIA:
[CRÍTICA / ALTA / MEDIA]
MOTIVO DE RELEVANCIA:
Una sola frase explicando por qué debe conservarse.
TRANSCRIPCIÓN:
[transcripción original completa]
==================================================
REGLA ESPECIAL SOBRE JOH
Una noticia que hable de política hondureña en general NO debe incluirse simplemente porque pueda tener alguna relación indirecta con JOH.
Debe existir una conexión clara con:

* JOH;
* su proceso;
* Aspra;
* PGR;
* Guías de Familia / Vida Mejor;
* o una narrativa directamente relacionada con alguno de ellos.

REGLA ESPECIAL SOBRE GUÍAS DE FAMILIA
Presta especial atención a noticias que puedan mostrar una relación entre:
Guías de Familia → demanda/proceso → PGR → presión política → JOH
Pero NO establezcas tú esa relación si la noticia no la establece.
Si la conexión aparece explícitamente en la transcripción, conserva el contenido completo.
REGLA DE NO INVENCIÓN
Esta es una tarea de extracción, no de investigación.
NO utilices información externa.
No hagas búsquedas en Internet.
No agregues información que no aparezca en las transcripciones.
No completes información que falte.
Si no aparece un dato, escribe:
[NO INDICADO EN LA TRANSCRIPCIÓN]

NOTA SOBRE ESTE ENVÍO: lo que recibas a continuación es UN FRAGMENTO del archivo
semanal completo, no el archivo entero (el archivo se dividió en varios envíos
por límite de tamaño). Por lo tanto:
- NO generes el encabezado "EXTRACTO DE NOTICIAS RELEVANTES..." ni el conteo
  total de noticias analizadas/seleccionadas -- eso se arma después, uniendo
  todos los fragmentos.
- Empieza directamente con "NOTICIA 1" (la numeración se corrige después al unir).
- Si en este fragmento no hay ninguna noticia relevante, responde únicamente
  con la línea: SIN NOTICIAS RELEVANTES EN ESTE FRAGMENTO
"""

BLOQUE_RE = re.compile(r"(?m)^=+$")
SEP_RE = re.compile(r"(?m)^-+$")


def _ahora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _openai() -> OpenAI:
    if not settings.openai_api_key:
        raise SystemExit("falta OPENAI_API_KEY")
    return OpenAI(api_key=settings.openai_api_key.get_secret_value())


def _openai2() -> OpenAI:
    key = _leer_env_var("OPENAI_API_KEY_2")
    if not key:
        raise SystemExit("falta OPENAI_API_KEY_2 en .env")
    return OpenAI(api_key=key)


def _estado(nuevo: dict | None = None) -> dict:
    if nuevo is not None:
        SALIDA.mkdir(parents=True, exist_ok=True)
        ESTADO.write_text(json.dumps(nuevo, ensure_ascii=False, indent=1), encoding="utf-8")
        return nuevo
    return json.loads(ESTADO.read_text(encoding="utf-8")) if ESTADO.exists() else {}


def leer_bloques() -> list[str]:
    """Cada bloque conserva su delimitador y formato original tal cual esta
    en el archivo, para no alterar el texto que se le manda al modelo."""
    crudo = ENTRADA.read_text(encoding="utf-8")
    piezas = BLOQUE_RE.split(crudo)
    delim = "=" * 78
    bloques = []
    for pieza in piezas:
        pieza = pieza.strip("\n")
        if not pieza.strip():
            continue
        bloques.append(f"{delim}\n{pieza.strip()}\n")
    return bloques


def armar_chunks(bloques: list[str], presupuesto: int) -> list[list[str]]:
    chunks, actual, tam = [], [], 0
    for b in bloques:
        if actual and tam + len(b) > presupuesto:
            chunks.append(actual)
            actual, tam = [], 0
        actual.append(b)
        tam += len(b)
    if actual:
        chunks.append(actual)
    return chunks


# --------------------------------------------------------------------- submit

def submit() -> None:
    """Manda la primera mitad de los chunks a la cuenta principal de OpenAI.
    La segunda mitad queda pendiente hasta correr --reenviar-restantes-openai2
    (para mandarla a una segunda cuenta y correr las dos colas en paralelo)."""
    if not ENTRADA.exists():
        raise SystemExit(f"no existe {ENTRADA}")
    bloques = leer_bloques()
    chunks = armar_chunks(bloques, PRESUPUESTO_CHARS)
    mitad = len(chunks) // 2
    idx_openai = list(range(0, mitad))
    idx_anthropic = list(range(mitad, len(chunks)))
    print(f"{len(bloques)} noticias | {len(chunks)} chunks total "
          f"({len(idx_openai)} a OpenAI/{MODELO_OPENAI}, "
          f"{len(idx_anthropic)} pendientes de la segunda cuenta)")

    conteo_por_chunk = {f"chunk_{i:04d}": len(chunks[i]) for i in range(len(chunks))}

    # ---- OpenAI ----
    cli_oa = _openai()
    lineas_oa = []
    for i in idx_openai:
        cid = f"chunk_{i:04d}"
        texto = "\n".join(chunks[i])
        lineas_oa.append(json.dumps({
            "custom_id": cid,
            "method": "POST",
            "url": "/v1/chat/completions",
            "body": {
                "model": MODELO_OPENAI,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": texto},
                ],
            },
        }, ensure_ascii=False))

    SALIDA.mkdir(parents=True, exist_ok=True)
    jsonl_oa = SALIDA / "peticiones_openai.jsonl"
    jsonl_oa.write_text("\n".join(lineas_oa), encoding="utf-8")
    subido = cli_oa.files.create(file=jsonl_oa.open("rb"), purpose="batch")
    batch_oa = cli_oa.batches.create(
        input_file_id=subido.id,
        endpoint="/v1/chat/completions",
        completion_window="24h",
        metadata={"tarea": "filtro-joh-semana-3al11"},
    )
    print(f"OpenAI batch {batch_oa.id} | estado {batch_oa.status}")

    t_creacion = _ahora()
    _estado({
        "total_noticias": len(bloques), "chunks": len(chunks),
        "conteo_por_chunk": conteo_por_chunk,
        "idx_pendiente_segunda_cuenta": idx_anthropic,
        "t_creacion": t_creacion,
        "openai": {
            "batch_id": batch_oa.id, "modelo": MODELO_OPENAI,
            "transiciones": [[t_creacion, batch_oa.status]],
        },
    })


def reenviar_restantes_openai2() -> None:
    """Manda a la segunda cuenta de OpenAI los chunks que iban a Anthropic
    (cancelado). Reconstruye los mismos chunks desde el archivo original para
    que los indices coincidan exactamente con los que ya se guardaron."""
    est = _estado()
    if not est or "idx_pendiente_segunda_cuenta" not in est:
        raise SystemExit("no hay chunks pendientes -- corre --submit primero")

    bloques = leer_bloques()
    chunks = armar_chunks(bloques, PRESUPUESTO_CHARS)
    idx = est["idx_pendiente_segunda_cuenta"]
    print(f"reenviando {len(idx)} chunks a la segunda cuenta de OpenAI")

    cli2 = _openai2()
    lineas = []
    for i in idx:
        cid = f"chunk_{i:04d}"
        texto = "\n".join(chunks[i])
        lineas.append(json.dumps({
            "custom_id": cid,
            "method": "POST",
            "url": "/v1/chat/completions",
            "body": {
                "model": MODELO_OPENAI,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": texto},
                ],
            },
        }, ensure_ascii=False))

    jsonl2 = SALIDA / "peticiones_openai2.jsonl"
    jsonl2.write_text("\n".join(lineas), encoding="utf-8")
    subido = cli2.files.create(file=jsonl2.open("rb"), purpose="batch")
    batch2 = cli2.batches.create(
        input_file_id=subido.id,
        endpoint="/v1/chat/completions",
        completion_window="24h",
        metadata={"tarea": "filtro-joh-semana-3al11-cuenta2"},
    )
    print(f"OpenAI (cuenta 2) batch {batch2.id} | estado {batch2.status}")

    t_creacion = _ahora()
    est["openai2"] = {
        "batch_id": batch2.id, "modelo": MODELO_OPENAI,
        "transiciones": [[t_creacion, batch2.status]],
    }
    del est["idx_pendiente_segunda_cuenta"]
    _estado(est)


# --------------------------------------------------------------------- seguir

def seguir(intervalo: int = 30) -> None:
    est = _estado()
    if not est:
        raise SystemExit("no hay batch: corre --submit primero")
    if "openai2" not in est:
        raise SystemExit("falta --reenviar-restantes-openai2 antes de seguir")

    cli_oa, cli_oa2 = _openai(), _openai2()
    while True:
        oa_listo = _seguir_openai(cli_oa, est, "openai")
        oa2_listo = _seguir_openai(cli_oa2, est, "openai2")
        _estado(est)
        if oa_listo and oa2_listo:
            print("los dos batches terminaron")
            return
        time.sleep(intervalo)


def _seguir_openai(cli, est: dict, clave: str) -> bool:
    info = est[clave]
    if info.get("t_final"):
        return True
    b = cli.batches.retrieve(info["batch_id"])
    if b.status != info["transiciones"][-1][1]:
        info["transiciones"].append([_ahora(), b.status])
        print(f"[{clave}] {_ahora()}  ->  {b.status}   {b.request_counts}")
    if b.status in ("completed", "failed", "expired", "cancelled"):
        info["t_final"] = _ahora()
        info["output_file_id"] = b.output_file_id
        info["error_file_id"] = b.error_file_id
        return True
    return False


# -------------------------------------------------------------------- collect

def _extraer_texto_openai(cli, batch_id: str, clave: str = "openai") -> tuple[dict[str, str], list[str]]:
    por_chunk, errores = {}, []
    b = cli.batches.retrieve(batch_id)
    if not b.output_file_id:
        errores.append(f"{clave} batch {batch_id}: sin output_file_id (status={b.status})")
        return por_chunk, errores
    crudo = cli.files.content(b.output_file_id).text
    (SALIDA / f"resultados_{clave}.jsonl").write_text(crudo, encoding="utf-8")
    for linea in crudo.splitlines():
        if not linea.strip():
            continue
        r = json.loads(linea)
        cid = r["custom_id"]
        if r.get("error"):
            errores.append(f"{cid}: {r['error']}")
            continue
        cuerpo = (r.get("response") or {}).get("body") or {}
        if r.get("response", {}).get("status_code") != 200:
            errores.append(f"{cid}: http {r.get('response', {}).get('status_code')}")
            continue
        try:
            por_chunk[cid] = cuerpo["choices"][0]["message"]["content"]
        except Exception as exc:  # noqa: BLE001
            errores.append(f"{cid}: {exc}")
    if b.error_file_id:
        errores.append(f"openai error_file_id presente: {b.error_file_id}")
    return por_chunk, errores


def collect() -> None:
    est = _estado()
    if not est.get("openai", {}).get("t_final") or not est.get("openai2", {}).get("t_final"):
        raise SystemExit("algun batch no termino: corre --seguir")

    por_chunk_oa, err_oa = _extraer_texto_openai(_openai(), est["openai"]["batch_id"], "openai")
    por_chunk_oa2, err_oa2 = _extraer_texto_openai(_openai2(), est["openai2"]["batch_id"], "openai2")
    por_chunk = {**por_chunk_oa, **por_chunk_oa2}
    errores = err_oa + err_oa2

    noticias = []
    noticia_re = re.compile(
        r"=+\s*\nNOTICIA\s+\d+\s*\n(.*?)(?=\n=+\s*\nNOTICIA\s+\d+|\Z)", re.S
    )
    for idx in range(est["chunks"]):
        cid = f"chunk_{idx:04d}"
        texto = por_chunk.get(cid, "")
        if not texto:
            errores.append(f"{cid}: sin resultado de ningun proveedor")
            continue
        if "SIN NOTICIAS RELEVANTES" in texto[:80]:
            continue
        for m in noticia_re.finditer(texto):
            noticias.append(m.group(1).strip())

    total_analizadas = est["total_noticias"]
    total_seleccionadas = len(noticias)

    delim = "=" * 50
    partes = [
        delim,
        "EXTRACTO DE NOTICIAS RELEVANTES",
        "INFORME ESTRATÉGICO — JUAN ORLANDO HERNÁNDEZ",
        "PERÍODO:",
        PERIODO,
        "TOTAL DE NOTICIAS ANALIZADAS:",
        str(total_analizadas),
        "TOTAL DE NOTICIAS SELECCIONADAS:",
        str(total_seleccionadas),
        delim,
        "",
    ]
    for i, cuerpo in enumerate(noticias, start=1):
        partes.append(delim)
        partes.append(f"NOTICIA {i}")
        partes.append(cuerpo)
        partes.append(delim)
        partes.append("")

    final = SALIDA / "noticias_relevantes_semana.txt"
    final.write_text("\n".join(partes), encoding="utf-8")

    print(f"{total_seleccionadas} noticias relevantes de {total_analizadas} analizadas")
    if errores:
        print(f"\n{len(errores)} errores:")
        for e in errores[:20]:
            print(f"  {e}")
    print(f"\nescrito: {final}")


def main() -> None:
    global ENTRADA, SALIDA, ESTADO, PERIODO
    p = argparse.ArgumentParser()
    p.add_argument("--submit", action="store_true")
    p.add_argument("--reenviar-restantes-openai2", action="store_true")
    p.add_argument("--seguir", action="store_true")
    p.add_argument("--collect", action="store_true")
    p.add_argument("--entrada", type=Path, default=None, help="archivo de entrada (default: la semana 3-11 ago)")
    p.add_argument("--salida", type=Path, default=None, help="carpeta de estado/salida (default: data/filtro_joh_semana)")
    p.add_argument("--periodo", type=str, default=None, help="texto de PERÍODO en el encabezado final")
    a = p.parse_args()
    if a.entrada:
        ENTRADA = a.entrada
    if a.salida:
        SALIDA = a.salida
        ESTADO = SALIDA / "estado.json"
    if a.periodo:
        PERIODO = a.periodo

    if a.submit:
        submit()
    elif a.reenviar_restantes_openai2:
        reenviar_restantes_openai2()
    elif a.seguir:
        seguir()
    elif a.collect:
        collect()
    else:
        p.error("elegi --submit, --reenviar-restantes-openai2, --seguir o --collect")


if __name__ == "__main__":
    main()
