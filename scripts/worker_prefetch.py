"""Transcription Worker persistente con prefetch: mientras la GPU transcribe el
archivo N, un hilo en segundo plano descarga desde S3 el archivo N+1, para que
cuando termine la transcripcion el siguiente audio ya este en disco.

Misma logica que worker.py (modelo cargado una vez, loop sobre SQS), mas un
hilo productor que hace receive_message + download_file y entrega el resultado
por una Queue(maxsize=1) -- eso limita el prefetch a "un archivo por delante"
(no descarga N+2 hasta que N+1 fue consumido), evita descargas duplicadas
(cada mensaje se recibe una sola vez, siempre desde el hilo de prefetch) y
evita usar memoria de mas (nunca hay mas de un archivo prefetched en disco).

La transcripcion en si (motor, parametros, formato de salida) ya no vive en
este archivo -- se delega a FasterWhisperProvider (implementa
TranscriptionProvider, mismo patron que AIAnalysisProvider). Este script solo
coordina SQS/S3/prefetch y serializa el TranscriptionResult a los mismos dos
archivos de siempre (.txt legible + _words.json). Ver
docs/TRANSCRIPTION_ARCHITECTURE.md.

Manejo de errores: cualquier fallo (descarga, transcripcion, escritura, subida)
se clasifica como transitorio o permanente (src/shared/errors.py) y se delega
a DlqHandler -- permanente = a la DLQ de inmediato; transitorio = reintento con
backoff via ChangeMessageVisibility, con el RedrivePolicy de la cola
(maxReceiveCount=3) como respaldo final. Ver docs/ERROR_HANDLING.md.
"""
import json
import os
import queue
import sys
import threading
import time

import boto3

QUEUE_URL = os.environ["QUEUE_URL"]
DLQ_URL = os.environ.get("DLQ_URL", QUEUE_URL + "-dlq")
# Cola que consume TranscriptionResultConsumer en el backend (CPU, con
# Postgres) -- chepita nunca escribe a la DB directamente, solo publica aca
# despues de subir el resultado a S3. Ver docs/INGESTION_DESIGN.md.
DONE_QUEUE_URL = os.environ.get("DONE_QUEUE_URL")
WORKER_ID = os.environ.get("WORKER_ID", "w0")
MODEL_NAME = os.environ.get("WHISPER_MODEL", "small")
BATCH_SIZE = int(os.environ.get("WHISPER_BATCH_SIZE", "24"))
COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE_TYPE", "int8_float16")
WORK_DIR = f"/home/ubuntu/worker_bench_{WORKER_ID}"

# El shim de LD_LIBRARY_PATH es especifico de la AMI de chepita (ruta de
# cublas/cudnn que trae /opt/pytorch) -- se queda aqui, en el script de
# despliegue, y no dentro de FasterWhisperProvider, para que el provider siga
# siendo portable a cualquier otra maquina con faster-whisper instalado normal.
NVLIBDIR = "/opt/pytorch/lib/python3.13/site-packages/nvidia"
os.environ["LD_LIBRARY_PATH"] = (
    f"{NVLIBDIR}/cublas/lib:{NVLIBDIR}/cudnn/lib:" + os.environ.get("LD_LIBRARY_PATH", "")
)

# El arbol src/... se despliega junto a este script bajo /home/ubuntu/app/
# (ver docs/TRANSCRIPTION_ARCHITECTURE.md, seccion Despliegue).
sys.path.insert(0, "/home/ubuntu/app")

from src.modules.transcription.providers.faster_whisper_provider import FasterWhisperProvider  # noqa: E402
from src.modules.transcription.queue.dlq_handler import handle_failure  # noqa: E402
from src.shared.error_context import build_error_context  # noqa: E402
from src.shared.errors import classify_and_wrap  # noqa: E402
from src.shared.logging_utils import get_logger  # noqa: E402

os.makedirs(WORK_DIR, exist_ok=True)
sqs = boto3.client("sqs")
s3 = boto3.client("s3")
logger = get_logger(f"transcription_worker.{WORKER_ID}")


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(f"[{WORKER_ID}] {ts} {msg}", flush=True)


def _cleanup(*paths: str) -> None:
    for p in paths:
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass  # best-effort -- no tapar el error original por un fallo de limpieza


log("cargando modelo (una sola vez para todo el ciclo de vida del worker)...")
t_load = time.time()
transcription_provider = FasterWhisperProvider(
    model_name=MODEL_NAME, compute_type=COMPUTE_TYPE, batch_size=BATCH_SIZE,
)
log(f"modelo cargado en {time.time() - t_load:.1f}s, empezando a consumir la cola...")

prefetch_q: "queue.Queue" = queue.Queue(maxsize=1)
stop_event = threading.Event()


def prefetch_loop() -> None:
    while not stop_event.is_set():
        resp = sqs.receive_message(
            QueueUrl=QUEUE_URL, MaxNumberOfMessages=1, WaitTimeSeconds=10,
            AttributeNames=["ApproximateReceiveCount"],
        )
        messages = resp.get("Messages", [])
        if not messages:
            prefetch_q.put(None)
            continue

        msg = messages[0]
        job_id = msg["MessageId"]
        attempt = int(msg["Attributes"]["ApproximateReceiveCount"])

        job = None
        try:
            job = json.loads(msg["Body"])
            station = job["station"]
            t_dl = time.time()

            in_bucket, in_key = job["s3_input"].replace("s3://", "").split("/", 1)
            # Nombre unico por job, no por estacion -- con muchos jobs
            # seguidos de la misma estacion (caso normal en produccion), un
            # nombre fijo por estacion hace que el prefetch del job N+1
            # descargue sobre el mismo path que el _cleanup() del job N borra
            # justo despues, tumbando el archivo recien descargado antes de
            # usarse (visto en produccion: ~65% de fallos "No such file or
            # directory" al lanzar 4 instancias contra un solo canal).
            local_audio = f"{WORK_DIR}/{station}_{job_id}.mp3"
            s3.download_file(in_bucket, in_key, local_audio)
        except Exception as exc:
            audio_ref = job.get("s3_input") if isinstance(job, dict) else None
            error = classify_and_wrap(exc, module="download")
            ctx = build_error_context(error, module="download", job_id=job_id, audio_ref=audio_ref, attempt=attempt)
            handle_failure(sqs, QUEUE_URL, DLQ_URL, msg, job if job is not None else msg.get("Body", ""), error, ctx)
            log(f"FAILED download job_id={job_id} attempt={attempt} tipo={type(error).__name__}")
            continue

        log(f"PREFETCHED {station} download_elapsed={time.time() - t_dl:.1f}s")
        prefetch_q.put((msg, job, local_audio, job_id, attempt))


threading.Thread(target=prefetch_loop, daemon=True).start()

idle_rounds = 0
processed = 0
failed = 0
wait_for_download_total = 0.0
while idle_rounds < 3:
    t_wait = time.time()
    item = prefetch_q.get()
    waited = time.time() - t_wait

    if item is None:
        idle_rounds += 1
        continue
    idle_rounds = 0
    wait_for_download_total += waited

    msg, job, local_audio, job_id, attempt = item
    station = job["station"]
    s3_output_prefix = job["s3_output_prefix"]

    t0 = time.time()
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0))
    log(f"START {station} wait_for_download={waited:.2f}s")

    local_txt = f"{WORK_DIR}/{station}_{job_id}.txt"
    local_words = f"{WORK_DIR}/{station}_{job_id}_words.json"

    try:
        result = transcription_provider.transcribe(local_audio)

        with open(local_txt, "w", encoding="utf-8") as f:
            f.write(f"language={result.language} duration={result.duration} worker={WORKER_ID}\n\n")
            for seg in result.segments:
                f.write(f"[{seg.start:.1f}s -> {seg.end:.1f}s] {seg.text}\n")

        # words.json serializado desde el contrato unico (Word), no construido a
        # mano -- es el input del proximo paso del pipeline (segmentacion
        # narrativa via LLM por indice de palabra, no por segundos; ver
        # docs/PRD.md y docs/TRANSCRIPTION_ARCHITECTURE.md).
        with open(local_words, "w", encoding="utf-8") as f:
            json.dump([w.model_dump() for w in result.words], f, ensure_ascii=False)

        out_bucket, out_key = s3_output_prefix.replace("s3://", "").split("/", 1)
        s3.upload_file(local_txt, out_bucket, f"{out_key}.txt")
        s3.upload_file(local_words, out_bucket, f"{out_key}_words.json")
    except Exception as exc:
        error = classify_and_wrap(exc, module="transcribe_or_upload")
        ctx = build_error_context(
            error, module="transcribe_or_upload", job_id=job_id,
            audio_ref=job.get("s3_input"), attempt=attempt,
        )
        handle_failure(sqs, QUEUE_URL, DLQ_URL, msg, job, error, ctx)
        _cleanup(local_audio, local_txt, local_words)
        failed += 1
        log(f"FAILED {station} job_id={job_id} attempt={attempt} tipo={type(error).__name__}")
        continue

    _cleanup(local_audio, local_txt, local_words)

    elapsed = time.time() - t0
    processed += 1
    log(f"DONE {station} elapsed={elapsed:.1f}s")

    sqs.delete_message(QueueUrl=QUEUE_URL, ReceiptHandle=msg["ReceiptHandle"])

    grabacion_id = job.get("grabacion_id")
    if DONE_QUEUE_URL and grabacion_id:
        done_event = {
            "grabacion_id": grabacion_id,
            "station": station,
            "audio_s3_uri": job["s3_input"],
            "transcription_txt_s3_uri": f"s3://{out_bucket}/{out_key}.txt",
            "words_json_s3_uri": f"s3://{out_bucket}/{out_key}_words.json",
            "duration_seconds": result.duration,
            "language": result.language,
            "status": "completed",
            "worker_id": WORKER_ID,
            "started_at": started_at,
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        sqs.send_message(QueueUrl=DONE_QUEUE_URL, MessageBody=json.dumps(done_event, ensure_ascii=False))
    elif not grabacion_id:
        log(f"WARN {station} job sin grabacion_id -- no se publica evento done (job legado sin ingesta)")

stop_event.set()
log(f"cola vacia, saliendo. total procesados por este worker: {processed}, fallidos: {failed}, "
    f"tiempo total esperando descarga: {wait_for_download_total:.1f}s")
