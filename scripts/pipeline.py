"""Orquesta los dos pipelines de punta a punta: chepita -> segmentacion -> clipper.

    normal   chepita -> segmentacion BATCH  (2 cuentas OpenAI) -> clipper
    urgente  chepita -> segmentacion SYNC   (2 cuentas OpenAI) -> clipper

**La unica diferencia es el paso del medio.** Chepita y el clipper son
identicos; lo que cambia es como se paga la segmentacion:

  - BATCH: mitad de precio, latencia de horas y sin cota por abajo (medido el
    2026-08-18: 23 chunks no completaron ni uno en varios minutos). Sirve para
    drenar el backlog del dia sin apuro.
  - SYNC: precio completo, resultado en minutos. Sirve cuando hay que entregar
    un informe ya.

Por eso el normal corre por cron y el urgente se invoca a mano con una ventana
acotada: el urgente sobre todo el backlog seria caro sin necesidad.

**El paso de chepita no espera a que termine.** Solo se asegura de que haya
trabajo encolado y una instancia viva; el supervisor (infra/lambdas) ya la
apaga sola al vaciarse la cola. Esperar aca bloquearia el orquestador por
horas en el modo normal.

Uso:
    # urgente: lo de hoy desde las 04:00 GMT-6, con clips
    python scripts/pipeline.py --urgente --desde-hoy --con-clips

    # normal: drena el backlog pendiente
    python scripts/pipeline.py --normal --collect

    # normal, solo mandar los batches (el --collect va despues, por cron)
    python scripts/pipeline.py --normal --submit
"""
import argparse
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(RAIZ))

TZ_HN = timezone(timedelta(hours=-6))
PY = sys.executable


def log(msg: str) -> None:
    print(f"[{datetime.now(TZ_HN):%H:%M:%S}] {msg}", flush=True)


def _correr(script: str, *args: str) -> int:
    """Corre un script del pipeline heredando stdout/stderr. Devuelve el
    codigo de salida en vez de reventar: un paso que falla no debe impedir
    reportar lo que si avanzo."""
    cmd = [PY, str(RAIZ / "scripts" / script), *args]
    log(f"$ {script} {' '.join(args)}")
    return subprocess.run(cmd, cwd=RAIZ).returncode


# ------------------------------------------------------------------ chepita

def paso_chepita(esperar: bool) -> None:
    """Encola lo que falte transcribir y se asegura de que haya una chepita
    viva. No espera a que termine salvo que se pida (`--esperar`)."""
    import boto3

    from src.infrastructure.config import settings

    _correr("enqueue_transcriptions.py", "--limit", "500")

    sqs = boto3.client("sqs", region_name=settings.aws_region)
    attrs = sqs.get_queue_attributes(
        QueueUrl=settings.transcription_jobs_queue_url,
        AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
    )["Attributes"]
    pendientes = int(attrs["ApproximateNumberOfMessages"]) + int(
        attrs["ApproximateNumberOfMessagesNotVisible"]
    )
    if not pendientes:
        log("chepita: nada que transcribir")
        return

    ec2 = boto3.client("ec2", region_name=settings.aws_region)
    vivas = ec2.describe_instances(Filters=[
        {"Name": "tag:ManagedBy", "Values": ["cron-chepita"]},
        {"Name": "instance-state-name", "Values": ["running", "pending"]},
    ])["Reservations"]
    if vivas:
        log(f"chepita: {pendientes} en cola, ya hay instancia viva (el supervisor la cuida)")
    else:
        log(f"chepita: {pendientes} en cola, ninguna instancia -- lanzando")
        boto3.client("lambda", region_name=settings.aws_region).invoke(
            FunctionName="media-intel-chepita-launch", InvocationType="Event"
        )

    if not esperar:
        return

    log("esperando a que la cola de transcripcion se vacie...")
    while True:
        time.sleep(60)
        a = sqs.get_queue_attributes(
            QueueUrl=settings.transcription_jobs_queue_url,
            AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
        )["Attributes"]
        quedan = int(a["ApproximateNumberOfMessages"]) + int(a["ApproximateNumberOfMessagesNotVisible"])
        log(f"  cola: {quedan}")
        if quedan == 0:
            # El consumer del backend corre por cron cada minuto; se le da
            # margen para que las ultimas transcripciones lleguen a la DB
            # antes de segmentar, o el paso siguiente no las veria.
            log("cola vacia, esperando 90 s a que el consumer las persista")
            time.sleep(90)
            return


# ------------------------------------------------------------ segmentacion

def paso_segmentar_batch(submit: bool, collect: bool, limit: int) -> None:
    """BATCH: reparte los chunks entre las dos cuentas de OpenAI (un batch por
    cuenta) y despues los recolecta. Submit y collect estan separados porque
    entre uno y otro pasan horas."""
    if submit:
        _correr("segment_backlog_batch_openai.py", "--submit", "--limit", str(limit))
    if collect:
        _correr("segment_backlog_batch_openai.py", "--collect")


def paso_segmentar_sync(desde: str, hasta: str, medios: str, concurrencia: int) -> None:
    """SYNC: reparte por grabacion entre las dos cuentas, con un pool de hilos.
    Devuelve cuando termino todo."""
    args = ["--desde", desde, "--hasta", hasta, "--concurrencia", str(concurrencia)]
    if medios:
        args += ["--medios", medios]
    _correr("segmentar_ventana_sync.py", *args)


# ------------------------------------------------------------------ clipper

def paso_clipper(con_clips: bool, instancias: int, desde: str | None, hasta: str | None,
                 limit: int) -> None:
    """Lanza la flota de clippers efimeros. Usan reclamo atomico, no sharding:
    pueden arrancar escalonadas sin dejar huecos, que era el bug del sharding
    estatico (2026-08-18: 97 de 244 grabaciones sin procesar)."""
    args = ["--limit", str(limit), "--reclamo"]
    if con_clips:
        args.append("--con-clips")
    if desde:
        args += ["--fecha-desde", desde]
    if hasta:
        args += ["--fecha-hasta", hasta]

    if instancias <= 0:
        log("clipper: --instancias 0, corriendo local (no recomendado con clips)")
        _correr("process_cached_segments.py", *args)
        return

    from scripts.lanzar_clippers import lanzar

    lanzar(instancias=instancias, args_script=args)


# --------------------------------------------------------------------- main

def _ventana_hoy() -> tuple[str, str]:
    """De las 04:00 GMT-6 de hoy hasta ahora, en UTC con offset explicito.
    Las 04:00 locales son el arranque real de la programacion; antes de eso
    casi todo es repeticion."""
    ahora = datetime.now(TZ_HN)
    desde = ahora.replace(hour=4, minute=0, second=0, microsecond=0)
    if ahora < desde:  # antes de las 4am: la ventana es la de ayer
        desde -= timedelta(days=1)
    return (
        desde.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        ahora.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    modo = p.add_mutually_exclusive_group(required=True)
    modo.add_argument("--normal", action="store_true", help="segmentacion por BATCH (barato, lento)")
    modo.add_argument("--urgente", action="store_true", help="segmentacion SYNC (caro, minutos)")

    p.add_argument("--desde", help="ISO 8601 con Z u offset")
    p.add_argument("--hasta", help="ISO 8601 con Z u offset")
    p.add_argument("--desde-hoy", action="store_true",
                   help="ventana 04:00 GMT-6 de hoy -> ahora (atajo del urgente)")
    p.add_argument("--medios", default="", help="codigos separados por coma")

    p.add_argument("--submit", action="store_true", help="normal: solo mandar los batches")
    p.add_argument("--collect", action="store_true", help="normal: solo recolectar")
    p.add_argument("--limit", type=int, default=500)
    p.add_argument("--concurrencia", type=int, default=10)

    p.add_argument("--con-clips", action="store_true", help="cortar el audio y subirlo a S3")
    p.add_argument("--instancias", type=int, default=3,
                   help="clippers a lanzar (0 = correr local)")
    p.add_argument("--sin-chepita", action="store_true", help="saltar el paso de transcripcion")
    p.add_argument("--sin-clipper", action="store_true", help="saltar el paso de clipping")
    p.add_argument("--esperar-chepita", action="store_true",
                   help="bloquear hasta que la cola de transcripcion se vacie")
    a = p.parse_args()

    if a.desde_hoy:
        a.desde, a.hasta = _ventana_hoy()
    if a.urgente and not (a.desde and a.hasta):
        p.error("--urgente necesita --desde/--hasta o --desde-hoy: correrlo sobre todo el "
                "backlog costaria el doble sin necesidad (para eso esta --normal)")

    etiqueta = "URGENTE (sync)" if a.urgente else "NORMAL (batch)"
    log(f"=== PIPELINE {etiqueta} ===")
    if a.desde:
        log(f"ventana: {a.desde} -> {a.hasta}")

    if not a.sin_chepita:
        log("--- paso 1/3: chepita (transcripcion) ---")
        paso_chepita(esperar=a.esperar_chepita or a.urgente)

    log("--- paso 2/3: segmentacion ---")
    if a.urgente:
        paso_segmentar_sync(a.desde, a.hasta, a.medios, a.concurrencia)
    else:
        # Sin flags explicitos hace las dos cosas: recolecta lo que quedo de la
        # corrida anterior y manda lo nuevo.
        submit = a.submit or not (a.submit or a.collect)
        collect = a.collect or not (a.submit or a.collect)
        paso_segmentar_batch(submit, collect, a.limit)

    if not a.sin_clipper:
        log("--- paso 3/3: clipper ---")
        paso_clipper(a.con_clips, a.instancias, a.desde, a.hasta, a.limit)

    log(f"=== PIPELINE {etiqueta} TERMINADO ===")


if __name__ == "__main__":
    main()
