"""Supervisor de las chepitas del cron. Corre cada 5 min y hace las dos cosas
que hacen falta para que una instancia encendida nunca este ociosa con trabajo
en cola:

  cola vacia  -> termina las instancias que pasaron la gracia (ahorro)
  cola con trabajo -> se asegura de que tengan workers vivos (reinicio)

**Por que hace falta el reinicio.** `worker_prefetch.py` sale solo cuando ve la
cola vacia -- es deliberado, asi la instancia queda lista para apagarse. Pero si
entra trabajo nuevo entre que los workers mueren y que este supervisor apaga la
instancia, esa ventana quedaba muerta: instancia encendida, cobrando, sin nadie
consumiendo. Paso de verdad el 2026-08-18 (15 grabaciones esperando con 0
workers vivos sobre una instancia encendida).

El comando de reinicio es idempotente: `pgrep` decide, asi que mandarlo a una
instancia que ya esta trabajando no hace nada. Por eso no hace falta consultar
primero el estado y esperar la respuesta -- se manda y listo.
"""
import base64
from datetime import datetime, timedelta, timezone

import boto3

REGION = "us-east-1"
QUEUE_URL = "https://sqs.us-east-1.amazonaws.com/050871635829/media-intel-transcription-jobs"
DONE_QUEUE_URL = "https://sqs.us-east-1.amazonaws.com/050871635829/media-intel-transcription-done"

# Gracia antes de apagar: no matar una instancia que todavia esta cargando el
# modelo (~83 s en frio) y aun no pidio su primer mensaje.
GRACIA_APAGADO = timedelta(minutes=3)
# Gracia antes de supervisar: la lambda de lanzamiento tarda hasta ~5 min en
# dejar los workers corriendo (espera running + SSM Online). Tocar antes seria
# competir con ella.
GRACIA_REINICIO = timedelta(minutes=6)

ARRANCAR_SI_HACEN_FALTA = f"""
if pgrep -f worker_prefetch >/dev/null 2>&1; then
  echo "workers vivos, nada que hacer"
  exit 0
fi
echo "sin workers y hay cola -- relanzando"
export QUEUE_URL="{QUEUE_URL}"
export DONE_QUEUE_URL="{DONE_QUEUE_URL}"
export WHISPER_MODEL=large-v3-turbo
export WHISPER_COMPUTE_TYPE=int8_float16
export WHISPER_BATCH_SIZE=12
export AWS_DEFAULT_REGION=us-east-1
mkdir -p /home/ubuntu/run_logs
for i in 0 1 2; do
  WORKER_ID=w$i setsid nohup /opt/pytorch/bin/python3 /home/ubuntu/worker_prefetch.py \
    < /dev/null >> /home/ubuntu/run_logs/worker_w$i.log 2>&1 &
done
sleep 3
echo "workers ahora: $(pgrep -fc worker_prefetch)"
date -u +"reinicio %Y-%m-%dT%H:%M:%SZ" >> /home/ubuntu/run_logs/reinicios.log
"""


def handler(event, context):
    ec2 = boto3.client("ec2", region_name=REGION)
    sqs = boto3.client("sqs", region_name=REGION)
    ssm = boto3.client("ssm", region_name=REGION)

    attrs = sqs.get_queue_attributes(
        QueueUrl=QUEUE_URL,
        AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
    )["Attributes"]
    visibles = int(attrs["ApproximateNumberOfMessages"])
    en_vuelo = int(attrs["ApproximateNumberOfMessagesNotVisible"])

    resp = ec2.describe_instances(Filters=[
        {"Name": "tag:ManagedBy", "Values": ["cron-chepita"]},
        {"Name": "instance-state-name", "Values": ["running"]},
    ])
    instancias = [i for r in resp["Reservations"] for i in r["Instances"]]
    ahora = datetime.now(timezone.utc)

    if not instancias:
        print(f"sin chepitas del cron (cola: {visibles} visibles / {en_vuelo} en vuelo)")
        return {"apagadas": [], "revisadas": []}

    # ------------------------------------------------------------ apagado
    if visibles == 0 and en_vuelo == 0:
        a_terminar = [i["InstanceId"] for i in instancias
                      if ahora - i["LaunchTime"] >= GRACIA_APAGADO]
        if a_terminar:
            ec2.terminate_instances(InstanceIds=a_terminar)
            print(f"cola vacia -- terminando: {a_terminar}")
        else:
            print("cola vacia pero ninguna paso la gracia de apagado")
        return {"apagadas": a_terminar, "revisadas": []}

    # ----------------------------------------------------------- reinicio
    revisadas = [i["InstanceId"] for i in instancias
                 if ahora - i["LaunchTime"] >= GRACIA_REINICIO]
    if not revisadas:
        print(f"hay cola ({visibles}/{en_vuelo}) pero las instancias son muy nuevas "
              f"-- la lambda de lanzamiento todavia puede estar arrancandolas")
        return {"apagadas": [], "revisadas": []}

    ssm.send_command(
        InstanceIds=revisadas,
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": [
            "echo " + base64.b64encode(ARRANCAR_SI_HACEN_FALTA.encode()).decode()
            + " | base64 -d > /tmp/supervisar_workers.sh",
            "bash /tmp/supervisar_workers.sh",
        ]},
        Comment="supervisor chepita: relanzar workers si hacen falta",
    )
    print(f"hay cola ({visibles} visibles / {en_vuelo} en vuelo) -- "
          f"comando de supervision enviado a {revisadas}")
    return {"apagadas": [], "revisadas": revisadas}
