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
# Techo duro de ociosidad: si no hay mensajes VISIBLES durante este tiempo, se
# apaga aunque el contador de "en vuelo" diga que hay trabajo.
#
# Por que hace falta: un mensaje en vuelo que nadie procesa es indistinguible,
# desde SQS, de uno que si se esta procesando. El 2026-08-19 quedaron 14
# mensajes fantasma (workers que murieron sin devolverlos) y la condicion
# `en_vuelo == 0` no se cumplio nunca: 3 g6.xlarge encendidas 11 h con la GPU
# al 0%, ~$16. El worker ya no los abandona (ver `_devolver` en
# worker_prefetch.py), pero el supervisor no debe poder sostener una flota de
# GPU indefinidamente por un contador que se traba.
#
# El umbral es mayor que el VisibilityTimeout de la cola (30 min) a proposito:
# un mensaje en vuelo de verdad, si su worker murio, vuelve a estar VISIBLE
# antes de los 30 min. Entonces "35 min sin ver un solo mensaje visible"
# implica que no queda trabajo real -- ni en proceso ni abandonado.
TECHO_OCIOSIDAD = timedelta(minutes=35)
# Tope duro de vida para una chepita del cron, independiente de cualquier
# senal de la cola. Es el ultimo cinturon: si la logica de arriba falla por un
# motivo que no previmos, esto acota el gasto a ~$4.8 por instancia en vez de
# dejarla encendida indefinidamente.
#
# Solo aplica a las del cron (tag ManagedBy=cron-chepita). Las que se lanzan a
# mano para un backfill largo no llevan ese tag y no se ven afectadas.
VIDA_MAXIMA = timedelta(hours=6)
# Donde se guarda "la ultima vez que hubo trabajo visible". La lambda es sin
# estado y corre cada 5 min, asi que necesita persistir el dato en algun lado;
# Parameter Store alcanza y no cuesta nada a esta escala.
PARAM_ULTIMO_TRABAJO = "/media-intel/chepita/ultimo-trabajo-visible"

# Mismo criterio que la lambda de lanzamiento: worker fresco de S3 si se puede,
# el de la AMI si no. Ver el comentario en chepita_launch/lambda_function.py.
WORKER_S3 = "s3://media-intel-transcribe-050871635829/deploy/worker_prefetch.py"

ARRANCAR_SI_HACEN_FALTA = f"""
if pgrep -f worker_prefetch >/dev/null 2>&1; then
  echo "workers vivos, nada que hacer"
  exit 0
fi
echo "sin workers y hay cola -- relanzando"
if aws s3 cp {WORKER_S3} /tmp/worker_prefetch.py --region us-east-1 2>/dev/null \
   && /opt/pytorch/bin/python3 -m py_compile /tmp/worker_prefetch.py 2>/dev/null; then
  cp /tmp/worker_prefetch.py /home/ubuntu/worker_prefetch.py
  echo "worker actualizado desde S3"
fi
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


def _marcar_ociosidad(ssm, visibles: int, ahora: datetime) -> datetime | None:
    """Lleva la cuenta de desde cuando no hay mensajes visibles.

    Devuelve el instante en que empezo la ociosidad, o None si ahora mismo hay
    trabajo visible. Guarda el dato en Parameter Store porque la lambda es sin
    estado entre invocaciones.
    """
    if visibles > 0:
        ssm.put_parameter(
            Name=PARAM_ULTIMO_TRABAJO, Value=ahora.isoformat(),
            Type="String", Overwrite=True,
        )
        return None

    try:
        crudo = ssm.get_parameter(Name=PARAM_ULTIMO_TRABAJO)["Parameter"]["Value"]
        return datetime.fromisoformat(crudo)
    except ssm.exceptions.ParameterNotFound:
        # Primera corrida sin trabajo: sembrar ahora para que el techo se mida
        # desde este momento y no apague algo recien lanzado.
        ssm.put_parameter(
            Name=PARAM_ULTIMO_TRABAJO, Value=ahora.isoformat(),
            Type="String", Overwrite=True,
        )
        return ahora
    except (ValueError, KeyError):
        return None


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

    # Antes del return temprano: el reloj de ociosidad se lleva siempre, haya o
    # no instancias. Si solo se actualizara con la flota encendida, el
    # timestamp quedaria congelado en el pasado y la primera chepita que
    # arranque se veria "ociosa hace horas" y se apagaria sola.
    ociosa_desde = _marcar_ociosidad(ssm, visibles, ahora)

    if not instancias:
        print(f"sin chepitas del cron (cola: {visibles} visibles / {en_vuelo} en vuelo)")
        return {"apagadas": [], "revisadas": []}

    # --------------------------------------------------------- tope de vida
    viejas = [i["InstanceId"] for i in instancias
              if ahora - i["LaunchTime"] >= VIDA_MAXIMA]
    if viejas:
        ec2.terminate_instances(InstanceIds=viejas)
        print(f"ATENCION tope de vida ({VIDA_MAXIMA}) alcanzado -- terminando: {viejas}. "
              f"cola: {visibles} visibles / {en_vuelo} en vuelo")
        instancias = [i for i in instancias if i["InstanceId"] not in viejas]
        if not instancias:
            return {"apagadas": viejas, "revisadas": []}

    # ------------------------------------------------------------ apagado
    #
    # Dos caminos al apagado:
    #   1. cola limpia de verdad (nada visible NI en vuelo)
    #   2. nada visible desde hace mas de TECHO_OCIOSIDAD -- el en_vuelo que
    #      quede son mensajes fantasma, no trabajo real
    ocio = ahora - ociosa_desde if ociosa_desde else timedelta(0)
    cola_limpia = visibles == 0 and en_vuelo == 0
    ocio_excedido = visibles == 0 and ocio >= TECHO_OCIOSIDAD

    if cola_limpia or ocio_excedido:
        a_terminar = [i["InstanceId"] for i in instancias
                      if ahora - i["LaunchTime"] >= GRACIA_APAGADO]
        if a_terminar:
            ec2.terminate_instances(InstanceIds=a_terminar)
            motivo = ("cola vacia" if cola_limpia else
                      f"sin trabajo visible hace {ocio.total_seconds() / 60:.0f} min "
                      f"({en_vuelo} en vuelo fantasma)")
            print(f"{motivo} -- terminando: {a_terminar}")
        else:
            print("apagable pero ninguna paso la gracia de apagado")
        return {"apagadas": a_terminar + viejas, "revisadas": []}

    # ----------------------------------------------------------- reinicio
    revisadas = [i["InstanceId"] for i in instancias
                 if ahora - i["LaunchTime"] >= GRACIA_REINICIO]
    if not revisadas:
        print(f"hay cola ({visibles}/{en_vuelo}) pero las instancias son muy nuevas "
              f"-- la lambda de lanzamiento todavia puede estar arrancandolas")
        return {"apagadas": viejas, "revisadas": []}

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
    return {"apagadas": viejas, "revisadas": revisadas}
