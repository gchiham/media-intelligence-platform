"""Lanza 1 chepita (g6.xlarge, AMI CHEPITA-L4-v1.3.0) y arranca sus 3 workers.

Disparado por 4 reglas de EventBridge (10am/3pm/8pm/11pm GMT-6). No espera a
que termine el trabajo -- eso lo decide `chepita_supervisor`, que corre aparte
cada 5 min: apaga la instancia cuando la cola queda vacia, o le relanza los
workers si murieron con trabajo pendiente.

Sin `--subnet-id`: dejar que AWS elija la AZ evita el `InsufficientInstanceCapacity`
que da lanzar contra una AZ fija (ver docs/INFRASTRUCTURE.md). Aun asi se
reintenta un par de veces por si la capacidad esta ajustada en todas las AZs
al mismo tiempo.
"""
import base64
import time

import boto3
from botocore.exceptions import ClientError

REGION = "us-east-1"
AMI_ID = "ami-0333472587024212a"  # CHEPITA-L4-v1.3.0
INSTANCE_TYPE = "g6.xlarge"
IAM_PROFILE = "media-intel-ec2-transcribe"
SECURITY_GROUP = "sg-033ac2dd79d76f56a"

QUEUE_URL = "https://sqs.us-east-1.amazonaws.com/050871635829/media-intel-transcription-jobs"
DONE_QUEUE_URL = "https://sqs.us-east-1.amazonaws.com/050871635829/media-intel-transcription-done"

# El worker viene horneado en la AMI, pero se intenta bajar la version fresca
# de S3 antes de arrancar: asi un arreglo del worker se despliega subiendo un
# archivo, sin rehornear la AMI (que son ~40 min). Si la descarga falla se sigue
# con la copia de la AMI -- nunca dejar la instancia sin worker por esto.
WORKER_S3 = "s3://media-intel-transcribe-050871635829/deploy/worker_prefetch.py"

BAJAR_WORKER = f"""
if aws s3 cp {WORKER_S3} /tmp/worker_prefetch.py --region us-east-1 2>/dev/null; then
  if /opt/pytorch/bin/python3 -m py_compile /tmp/worker_prefetch.py 2>/dev/null; then
    cp /tmp/worker_prefetch.py /home/ubuntu/worker_prefetch.py
    echo "worker actualizado desde S3"
  else
    echo "ATENCION worker de S3 no compila -- se usa el de la AMI"
  fi
else
  echo "sin worker en S3 -- se usa el de la AMI"
fi
"""

START_WORKERS_SCRIPT = f"""
{BAJAR_WORKER}
export QUEUE_URL="{QUEUE_URL}"
export DONE_QUEUE_URL="{DONE_QUEUE_URL}"
export WHISPER_MODEL=large-v3-turbo
export WHISPER_COMPUTE_TYPE=int8_float16
export WHISPER_BATCH_SIZE=12
export AWS_DEFAULT_REGION=us-east-1
mkdir -p /home/ubuntu/run_logs
for i in 0 1 2; do
  WORKER_ID=w$i setsid nohup /opt/pytorch/bin/python3 /home/ubuntu/worker_prefetch.py \\
    < /dev/null > /home/ubuntu/run_logs/worker_w$i.log 2>&1 &
done
sleep 2
ps aux | grep worker_prefetch | grep -v grep
"""


def _run_instance(ec2):
    return ec2.run_instances(
        ImageId=AMI_ID,
        InstanceType=INSTANCE_TYPE,
        IamInstanceProfile={"Name": IAM_PROFILE},
        SecurityGroupIds=[SECURITY_GROUP],
        MinCount=1,
        MaxCount=1,
        TagSpecifications=[
            {"ResourceType": "instance", "Tags": [
                {"Key": "Project", "Value": "media-intel"},
                {"Key": "Name", "Value": "media-intel-chepita-cron"},
                {"Key": "ManagedBy", "Value": "cron-chepita"},
            ]},
            {"ResourceType": "volume", "Tags": [{"Key": "Project", "Value": "media-intel"}]},
            {"ResourceType": "network-interface", "Tags": [{"Key": "Project", "Value": "media-intel"}]},
        ],
    )


def handler(event, context):
    ec2 = boto3.client("ec2", region_name=REGION)
    ssm = boto3.client("ssm", region_name=REGION)

    reservation = None
    ultimo_error = None
    for intento in range(3):
        try:
            reservation = _run_instance(ec2)
            break
        except ClientError as exc:
            ultimo_error = exc
            if "InsufficientInstanceCapacity" not in str(exc):
                raise
            time.sleep(10)
    if reservation is None:
        raise RuntimeError(f"sin capacidad g6.xlarge tras 3 intentos: {ultimo_error}")

    instance_id = reservation["Instances"][0]["InstanceId"]
    print(f"instancia lanzada: {instance_id}")

    # Esperar running + SSM Online (hasta ~4 min, deja margen al timeout de 5 min).
    waiter = ec2.get_waiter("instance_running")
    waiter.wait(InstanceIds=[instance_id], WaiterConfig={"Delay": 10, "MaxAttempts": 18})

    online = False
    for _ in range(24):  # hasta 4 min
        info = ssm.describe_instance_information(
            Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
        )
        estados = info.get("InstanceInformationList", [])
        if estados and estados[0]["PingStatus"] == "Online":
            online = True
            break
        time.sleep(10)

    if not online:
        raise RuntimeError(f"{instance_id}: SSM no llego a Online a tiempo")

    resultado = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": [
            "echo " + base64.b64encode(START_WORKERS_SCRIPT.encode()).decode() + " | base64 -d > /tmp/start_workers.sh",
            "bash /tmp/start_workers.sh",
        ]},
    )
    command_id = resultado["Command"]["CommandId"]
    print(f"comando de arranque de workers enviado: {command_id}")

    return {"instance_id": instance_id, "command_id": command_id}
