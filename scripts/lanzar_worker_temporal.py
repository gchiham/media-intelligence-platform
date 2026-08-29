"""Lanza una instancia EC2 efimera para trabajo pesado, con muerte automatica.

**Por que existe.** El backend es un t3.small de 2 vCPU que suele tener los
creditos de CPU en cero y ya corre el clipper: cualquier trabajo intensivo ahi
degrada el pipeline. Lo correcto es levantar algo grande un rato y matarlo.

**Por que se mata sola.** El 2026-08-29 una de estas quedo encendida 6.6 h por
$2.23 cuando el trabajo real tomo 4 minutos: el proceso murio y nadie lo noto.
Ahora la instancia trae un `shutdown -h +N` programado desde el arranque, y
`InstanceInitiatedShutdownBehavior=terminate`, asi que se autodestruye pase lo
que pase -- incluso si se pierde la conexion, si el script falla, o si quien la
lanzo se olvida de ella. Es un tope, no un reemplazo de apagarla al terminar.

Hereda el security group y el perfil IAM del backend a proposito: asi alcanza
la RDS (que no es publica) y el bucket de clips SIN tocar ninguna regla de
seguridad de produccion.

Uso:
    python scripts/lanzar_worker_temporal.py --minutos 60
    python scripts/lanzar_worker_temporal.py --tipo c5.4xlarge --minutos 30
    python scripts/lanzar_worker_temporal.py --listar
    python scripts/lanzar_worker_temporal.py --matar i-0abc...
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import boto3  # noqa: E402

# Copiados del backend de produccion (i-056c6415331779092): misma subnet, mismo
# SG y mismo perfil IAM es lo que da acceso a RDS y S3 sin abrir nada nuevo.
SUBNET = "subnet-f67ee591"
SECURITY_GROUP = "sg-0b0ba7ad930b6632b"
PERFIL_IAM = "media-intel-backend"
LLAVE = "keySED"
REGION = "us-east-1"
ETIQUETA = "worker-temporal"

# Ubuntu limpio y no la AMI del clipper: instalar cuatro dependencias es mas
# barato que heredar crons y configuracion de otra maquina.
FILTRO_AMI = "ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"
DUENO_UBUNTU = "099720109477"

PAQUETES = "pdfplumber boto3 sqlalchemy 'psycopg[binary]' pydantic pydantic-settings uuid6"


def _user_data(minutos: int) -> str:
    return f"""#!/bin/bash
# Tope de vida: se apaga sola pase lo que pase. Con
# InstanceInitiatedShutdownBehavior=terminate esto la destruye, no la deja
# parada facturando el disco.
shutdown -h +{minutos}

apt-get update -y
apt-get install -y python3-pip python3-venv
python3 -m venv /opt/v
/opt/v/bin/pip install -q --upgrade pip
/opt/v/bin/pip install -q {PAQUETES}
touch /home/ubuntu/LISTO
chown ubuntu:ubuntu /home/ubuntu/LISTO
"""


def _ec2():
    return boto3.client("ec2", region_name=REGION)


def lanzar(tipo: str, minutos: int, disco: int) -> None:
    ec2 = _ec2()
    imgs = ec2.describe_images(
        Owners=[DUENO_UBUNTU],
        Filters=[
            {"Name": "name", "Values": [FILTRO_AMI]},
            {"Name": "state", "Values": ["available"]},
        ],
    )["Images"]
    ami = sorted(imgs, key=lambda x: x["CreationDate"])[-1]

    r = ec2.run_instances(
        ImageId=ami["ImageId"],
        InstanceType=tipo,
        MinCount=1,
        MaxCount=1,
        KeyName=LLAVE,
        SubnetId=SUBNET,
        SecurityGroupIds=[SECURITY_GROUP],
        IamInstanceProfile={"Name": PERFIL_IAM},
        BlockDeviceMappings=[
            {
                "DeviceName": "/dev/sda1",
                "Ebs": {"VolumeSize": disco, "VolumeType": "gp3", "DeleteOnTermination": True},
            }
        ],
        UserData=_user_data(minutos),
        InstanceInitiatedShutdownBehavior="terminate",
        TagSpecifications=[
            {
                "ResourceType": "instance",
                "Tags": [
                    {"Key": "Name", "Value": ETIQUETA},
                    {"Key": "Efimera", "Value": "si"},
                    {"Key": "MuereEnMinutos", "Value": str(minutos)},
                ],
            }
        ],
    )
    iid = r["Instances"][0]["InstanceId"]
    print(f"lanzada {iid} ({tipo}), se autodestruye en {minutos} min")
    print("esperando IP...")
    ec2.get_waiter("instance_running").wait(InstanceIds=[iid])
    i = ec2.describe_instances(InstanceIds=[iid])["Reservations"][0]["Instances"][0]
    print(f"  ip: {i.get('PublicIpAddress')}")
    print(f"  ssh -i ~/.ssh/{LLAVE} ubuntu@{i.get('PublicIpAddress')}")
    print("  esperar a que exista /home/ubuntu/LISTO antes de usarla")
    print(f"\n  al terminar: python {Path(__file__).name} --matar {iid}")


def listar() -> None:
    ec2 = _ec2()
    r = ec2.describe_instances(
        Filters=[
            {"Name": "tag:Efimera", "Values": ["si"]},
            {"Name": "instance-state-name", "Values": ["pending", "running", "stopping"]},
        ]
    )
    vivas = [i for res in r["Reservations"] for i in res["Instances"]]
    if not vivas:
        print("no hay workers temporales vivos")
        return
    for i in vivas:
        minutos = next(
            (t["Value"] for t in i.get("Tags", []) if t["Key"] == "MuereEnMinutos"), "?"
        )
        print(
            f"  {i['InstanceId']}  {i['InstanceType']:12} {i['State']['Name']:10} "
            f"lanzada {i['LaunchTime']:%Y-%m-%d %H:%M} UTC  tope {minutos} min"
        )


def matar(iid: str) -> None:
    _ec2().terminate_instances(InstanceIds=[iid])
    print(f"terminate enviado a {iid}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--tipo", default="c5.2xlarge")
    p.add_argument(
        "--minutos", type=int, default=60,
        help="tope de vida. Se autodestruye al cumplirse, haya terminado o no",
    )
    p.add_argument("--disco", type=int, default=30, help="GB de EBS")
    p.add_argument("--listar", action="store_true")
    p.add_argument("--matar", metavar="INSTANCE_ID")
    a = p.parse_args()

    if a.listar:
        listar()
    elif a.matar:
        matar(a.matar)
    else:
        if a.minutos < 5 or a.minutos > 240:
            sys.exit("--minutos fuera de rango razonable (5-240)")
        lanzar(a.tipo, a.minutos, a.disco)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
