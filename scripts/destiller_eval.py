"""Arma el set de evaluacion de Destiller: elige grabaciones y construye el
dashboard de validacion humana (data/destiller_eval/review.html).

Objetivo: producir el dato que hoy no existe. Nadie ha medido que porcentaje de
lo que llega al dashboard es basura que se colo, ni cuantas noticias reales se
perdieron -- sin ese numero no se puede decir si Destiller mejora algo, ni
elegir su umbral de confianza. El LLM propone la particion, un humano la valida
en `review.html`, y scripts/destiller_calibrate.py convierte esos veredictos en
precision/recall y en la curva de umbral.

**Por que en tres pasos.** El destilado no corre aca sino en la instancia GPU
(`media-intel-destiller`, ver docs/INFRASTRUCTURE.md): su security group no
expone ningun puerto y abrirlo seria peor -- el usuario IAM del proyecto tiene
`AuthorizeSecurityGroupIngress` pero NO `Revoke`, asi que cada puerto que se
abre queda abierto para siempre. Mismo patron que los workers de chepita: el
trabajo va a la instancia, no al reves.

    1. python scripts/destiller_eval.py --paso seleccionar --limit 20
    2. (en la instancia) python destiller_worker.py     <- via SSM
    3. python scripts/destiller_eval.py --paso armar
"""
import argparse
import json
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import boto3  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

from src.infrastructure.config import settings  # noqa: E402

SALIDA = Path("data/destiller_eval")
PLANTILLA = Path(__file__).parent / "templates" / "destiller_review.html"
PREFIJO = "destiller_eval"

# Horas locales (GMT-6) que interesa cubrir: la proporcion de basura no es la
# misma en un noticiero matutino que en la programacion de la madrugada, y un
# set sesgado a una sola franja calibraria un umbral que no aplica al resto.
FRANJAS_LOCALES = [6, 9, 12, 15, 18, 21]
OFFSET_HONDURAS = -6

# transcripts/radio_america/2026-06-13T15Z_words.json
_LLAVE = re.compile(
    r"transcripts/(?P<medio>[^/]+)/(?P<stem>(?P<fecha>\d{4}-\d{2}-\d{2})T(?P<hora>\d{2})Z)_words\.json$"
)


@dataclass(frozen=True)
class Candidata:
    medio: str
    stem: str
    fecha: str
    hora_utc: int
    llave: str

    @property
    def hora_local(self) -> int:
        return (self.hora_utc + OFFSET_HONDURAS) % 24

    @property
    def id(self) -> str:
        return f"{self.medio}__{self.stem}"


def _s3():
    return boto3.client("s3", region_name=settings.aws_region)


# --------------------------------------------------------------- seleccionar

def _medios(s3) -> list[str]:
    resp = s3.list_objects_v2(
        Bucket=settings.transcribe_output_bucket, Prefix="transcripts/", Delimiter="/"
    )
    return sorted(p["Prefix"].split("/")[1] for p in resp.get("CommonPrefixes", []))


def _candidatas(s3, medio: str) -> list[Candidata]:
    salida = []
    paginador = s3.get_paginator("list_objects_v2")
    for pagina in paginador.paginate(
        Bucket=settings.transcribe_output_bucket, Prefix=f"transcripts/{medio}/"
    ):
        for obj in pagina.get("Contents", []):
            m = _LLAVE.match(obj["Key"])
            if m:
                salida.append(
                    Candidata(
                        medio=medio, stem=m["stem"], fecha=m["fecha"],
                        hora_utc=int(m["hora"]), llave=obj["Key"],
                    )
                )
    return salida


def _fecha_desc(c: Candidata) -> str:
    # Invierte el orden lexicografico de la fecha para que `min` prefiera la
    # mas reciente sin tener que parsearla.
    return "".join(chr(ord("9") - int(ch)) if ch.isdigit() else ch for ch in c.fecha)


def elegir(s3, limite: int) -> list[Candidata]:
    """Reparte las grabaciones entre medios y franjas horarias en vez de tomar
    las mas recientes, que caerian casi todas en el mismo noticiero."""
    medios = _medios(s3)
    if not medios:
        raise SystemExit("no hay transcripciones en S3")

    por_medio = {m: c for m, c in ((m, _candidatas(s3, m)) for m in medios) if c}
    medios = sorted(por_medio)

    elegidas: list[Candidata] = []
    usadas: set[str] = set()
    for i in range(limite):
        medio = medios[i % len(medios)]
        franja = FRANJAS_LOCALES[i % len(FRANJAS_LOCALES)]
        disponibles = [c for c in por_medio[medio] if c.id not in usadas]
        if not disponibles:
            continue
        mejor = min(
            disponibles,
            key=lambda c: (
                min(abs(c.hora_local - franja), 24 - abs(c.hora_local - franja)),
                _fecha_desc(c),
            ),
        )
        usadas.add(mejor.id)
        elegidas.append(mejor)
    return elegidas


def seleccionar(limite: int) -> None:
    s3 = _s3()
    print(f"eligiendo {limite} grabaciones...")
    elegidas = elegir(s3, limite)
    for c in elegidas:
        print(f"  {c.medio:22} {c.fecha}  {c.hora_local:02d}h local")

    payload = json.dumps([asdict(c) for c in elegidas], ensure_ascii=False, indent=1)
    SALIDA.mkdir(parents=True, exist_ok=True)
    (SALIDA / "seleccion.json").write_text(payload, encoding="utf-8")
    s3.put_object(
        Bucket=settings.transcribe_output_bucket,
        Key=f"{PREFIJO}/seleccion.json",
        Body=payload.encode("utf-8"),
    )
    print(f"\n{len(elegidas)} grabaciones -> s3://{settings.transcribe_output_bucket}/{PREFIJO}/seleccion.json")
    print("ahora corre destiller_worker.py en la instancia GPU (paso 2)")


# --------------------------------------------------------------------- armar

def _llave_audio(s3, c: Candidata) -> str | None:
    """El capturador cambio de formato y de nombre varias veces. Por orden de
    antiguedad inversa: `.ts` (transport stream, lo actual -- en TV trae video
    ademas del audio), luego `.mp3` con el mismo stem que la transcripcion, y
    por ultimo el nombre viejo `2026-06-12_23h.mp3`."""
    anio, mes = c.fecha[:4], c.fecha[5:7]
    for llave in (
        f"{c.medio}/{anio}/{mes}/{c.stem}.ts",
        f"{c.medio}/{anio}/{mes}/{c.stem}.mp3",
        f"{c.medio}/{anio}/{mes}/{c.fecha}_{c.hora_utc:02d}h.mp3",
    ):
        try:
            s3.head_object(Bucket=settings.capture_bucket, Key=llave)
            return llave
        except ClientError:
            continue
    return None


def _bajar_audio(s3, c: Candidata, destino: Path) -> bool:
    """Descarga y recomprime a mono 32 kbps. Una hora de aire pasa de ~29 MB a
    ~14 MB, y son 20 archivos que hay que abrir en un navegador."""
    if destino.exists():
        return True
    llave = _llave_audio(s3, c)
    if llave is None:
        return False

    destino.parent.mkdir(parents=True, exist_ok=True)
    # El sufijo tiene que ser el del origen: un .ts renombrado a .mp3 confunde
    # al demuxer de ffmpeg.
    with tempfile.NamedTemporaryFile(suffix=Path(llave).suffix, delete=False) as tmp:
        crudo = Path(tmp.name)
    try:
        s3.download_file(settings.capture_bucket, llave, str(crudo))
        proc = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(crudo),
             "-ac", "1", "-b:a", "32k", "-vn", str(destino)],
            capture_output=True,
        )
        if proc.returncode != 0:
            print(f"  ffmpeg fallo en {c.id}: {proc.stderr.decode(errors='replace')[:200]}")
            destino.unlink(missing_ok=True)
            return False
        return True
    finally:
        crudo.unlink(missing_ok=True)


def _destilado(s3, c: Candidata, etiqueta: str) -> dict | None:
    try:
        cuerpo = s3.get_object(
            Bucket=settings.transcribe_output_bucket,
            Key=f"{PREFIJO}/destilado/{etiqueta}/{c.id}.json",
        )["Body"].read()
    except ClientError:
        return None
    return json.loads(cuerpo)


def armar(con_audio: bool, workers: int, etiqueta: str) -> None:
    s3 = _s3()
    seleccion = [
        Candidata(**d) for d in json.loads((SALIDA / "seleccion.json").read_text(encoding="utf-8"))
    ]

    def una(c: Candidata) -> dict | None:
        propio = _s3()  # boto3 no garantiza clientes thread-safe
        d = _destilado(propio, c, etiqueta)
        if d is None:
            print(f"  falta el destilado de {c.id} (el worker no lo produjo)")
            return None
        audio = f"audio/{c.id}.mp3"
        if con_audio and not _bajar_audio(propio, c, SALIDA / audio):
            print(f"  sin audio para {c.id}: se revisa solo por texto")
            audio = None
        elif not con_audio:
            audio = None
        print(f"  {c.id}: {len(d['bloques'])} bloques")
        return {
            "id": c.id, "medio": c.medio, "fecha": c.fecha,
            "hora_utc": c.hora_utc, "hora_local": c.hora_local,
            "palabras": d["palabras"], "audio": audio, "bloques": d["bloques"],
        }

    with ThreadPoolExecutor(max_workers=workers) as pool:
        grabaciones = [g for g in pool.map(una, seleccion) if g is not None]

    if not grabaciones:
        raise SystemExit("no hay destilados en S3 todavia -- corre el paso 2 en la instancia")

    SALIDA.mkdir(parents=True, exist_ok=True)
    primero = _destilado(s3, seleccion[0], etiqueta) or {}
    bundle = {"modelo": primero.get("modelo", etiqueta), "grabaciones": grabaciones}
    (SALIDA / f"bundle_{etiqueta}.json").write_text(
        json.dumps(bundle, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    html = PLANTILLA.read_text(encoding="utf-8").replace(
        "/*__DATOS__*/null", json.dumps(bundle, ensure_ascii=False)
    )
    destino_html = SALIDA / f"review_{etiqueta}.html"
    destino_html.write_text(html, encoding="utf-8")

    total = sum(len(g["bloques"]) for g in grabaciones)
    print(f"\n{len(grabaciones)} grabaciones, {total} bloques por validar")
    print(f"abri: {destino_html.resolve()}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--paso", choices=["seleccionar", "armar"], required=True)
    parser.add_argument("--limit", type=int, default=20, help="grabaciones a evaluar")
    parser.add_argument("--sin-audio", action="store_true", help="no descarga ni recomprime audio")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--etiqueta",
        default="qwen2-5-7b-instruct",
        help="que destilado armar; permite comparar modelos sobre las mismas grabaciones",
    )
    args = parser.parse_args()

    if args.paso == "seleccionar":
        seleccionar(args.limit)
    else:
        armar(con_audio=not args.sin_audio, workers=args.workers, etiqueta=args.etiqueta)


if __name__ == "__main__":
    main()
