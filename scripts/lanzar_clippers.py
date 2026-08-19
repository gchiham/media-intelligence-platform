"""Lanza una flota efimera de clippers (c5.xlarge) que se aprovisionan solos.

Formaliza lo que se hacia a mano: empaquetar el repo, subirlo a S3, y lanzar N
instancias con un user-data que instala docker, buildea la imagen y corre
`process_cached_segments.py`. Ver docs/INFRASTRUCTURE.md (patron "Clipper").

**Las instancias usan reclamo atomico, no sharding.** Cada una pide la
siguiente grabacion libre con FOR UPDATE SKIP LOCKED, asi que pueden arrancar
escalonadas --lo normal, el aprovisionamiento tarda minutos-- sin dejar
huecos. El sharding estatico asumia que todas veian la misma lista y por eso
el 2026-08-18 quedaron 97 de 244 grabaciones sin procesar.

**Se apagan solas** cuando el backlog se agota: el user-data termina con un
`shutdown -h now` condicionado a que el script haya salido bien.

Uso:
    python scripts/lanzar_clippers.py --instancias 3 --con-clips
"""
import argparse
import base64
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(RAIZ))

import boto3  # noqa: E402

from src.infrastructure.config import settings  # noqa: E402

AMI_UBUNTU = "ami-052355af2a014bd2c"      # Ubuntu 24.04 plano, igual que el backend
TIPO = "c5.xlarge"
PERFIL_IAM = "media-intel-backend"        # ya tiene S3 de captura + clips
SG = "sg-0039ae8ea0cedf9f9"               # media-intel-clipper-sg (5432 -> RDS)
BUCKET_DEPLOY = "media-intel-clips-050871635829"
LLAVE_REPO = "deploy/repo_clipper.tar.gz"
LLAVE_ENV = "deploy/clipper.env"

# Solo lo que el Dockerfile necesita. El .env NO se incluye: los secretos van
# aparte (clipper.env), que cada instancia baja con su propio rol IAM en vez de
# viajar dentro de un artefacto copiado a N maquinas.
INCLUIR = ["Dockerfile", "pyproject.toml", "alembic.ini", "alembic", "src", "scripts", "docker"]

USER_DATA = """#!/bin/bash
exec > >(tee -a /var/log/clipper-boot.log | logger -t clipper-boot -s 2>/dev/console) 2>&1
set -x
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends docker.io unzip curl
snap install amazon-ssm-agent --classic 2>/dev/null || true
systemctl enable --now snap.amazon-ssm-agent.amazon-ssm-agent.service 2>/dev/null || true
curl -s "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscli.zip
unzip -q /tmp/awscli.zip -d /tmp && /tmp/aws/install
export PATH=/usr/local/bin:$PATH
systemctl enable --now docker

mkdir -p /opt/app
aws s3 cp "s3://__BUCKET__/__LLAVE_REPO__" /tmp/repo.tar.gz
aws s3 cp "s3://__BUCKET__/__LLAVE_ENV__" /opt/clipper.env
tar xzf /tmp/repo.tar.gz -C /opt/app
cd /opt/app
docker build -t clipper:latest .

# --entrypoint python3 sobreescribe el del backend (que levanta uvicorn): esta
# instancia no sirve HTTP, corre el script y termina.
docker run --name clipper --rm \\
  --env-file /opt/clipper.env \\
  --entrypoint python3 \\
  clipper:latest \\
  scripts/process_cached_segments.py __ARGS__
CODIGO=$?
echo "clipper termino con codigo $CODIGO"

# Auto-apagado: la instancia es efimera y no hay nadie mirandola. Solo si el
# script salio bien -- si fallo, se deja viva para poder diagnosticar por SSM.
if [ "$CODIGO" = "0" ]; then
  shutdown -h now
fi
"""


def empaquetar_y_subir(s3) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tar_path = Path(tmp) / "repo_clipper.tar.gz"
        with tarfile.open(tar_path, "w:gz") as tar:
            for nombre in INCLUIR:
                origen = RAIZ / nombre
                if origen.exists():
                    tar.add(origen, arcname=nombre,
                            filter=lambda t: None if "__pycache__" in t.name or t.name.endswith(".pyc") else t)
        s3.upload_file(str(tar_path), BUCKET_DEPLOY, LLAVE_REPO)
        print(f"repo subido: s3://{BUCKET_DEPLOY}/{LLAVE_REPO} ({tar_path.stat().st_size / 1024:.0f} KB)")


def lanzar(instancias: int, args_script: list[str], empaquetar: bool = True) -> list[str]:
    s3 = boto3.client("s3", region_name=settings.aws_region)
    ec2 = boto3.client("ec2", region_name=settings.aws_region)

    if empaquetar:
        empaquetar_y_subir(s3)

    try:
        s3.head_object(Bucket=BUCKET_DEPLOY, Key=LLAVE_ENV)
    except Exception:
        raise SystemExit(
            f"falta s3://{BUCKET_DEPLOY}/{LLAVE_ENV} -- es el archivo con DATABASE_URL "
            f"que baja cada clipper. Generarlo en el backend (donde vive el secreto) "
            f"y subirlo, nunca escribirlo a mano con el password en claro."
        )

    ud = (USER_DATA
          .replace("__BUCKET__", BUCKET_DEPLOY)
          .replace("__LLAVE_REPO__", LLAVE_REPO)
          .replace("__LLAVE_ENV__", LLAVE_ENV)
          .replace("__ARGS__", " ".join(args_script)))

    ids = []
    for i in range(instancias):
        r = ec2.run_instances(
            ImageId=AMI_UBUNTU, InstanceType=TIPO,
            IamInstanceProfile={"Name": PERFIL_IAM},
            SecurityGroupIds=[SG],
            UserData=ud, MinCount=1, MaxCount=1,
            # La instancia se apaga sola al terminar; con este flag ademas se
            # destruye, en vez de quedar `stopped` cobrando el disco.
            InstanceInitiatedShutdownBehavior="terminate",
            TagSpecifications=[
                {"ResourceType": "instance", "Tags": [
                    {"Key": "Project", "Value": "media-intel"},
                    {"Key": "Name", "Value": f"media-intel-clipper-{i}"},
                    {"Key": "Rol", "Value": "clipper"},
                ]},
                {"ResourceType": "volume", "Tags": [{"Key": "Project", "Value": "media-intel"}]},
                {"ResourceType": "network-interface", "Tags": [{"Key": "Project", "Value": "media-intel"}]},
            ],
        )
        iid = r["Instances"][0]["InstanceId"]
        ids.append(iid)
        print(f"  clipper {i}: {iid}")
        time.sleep(1)

    print(f"{len(ids)} clippers lanzados. Se aprovisionan en ~4-5 min y se apagan solos al terminar.")
    return ids


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--instancias", type=int, default=3)
    p.add_argument("--con-clips", action="store_true")
    p.add_argument("--limit", type=int, default=100000)
    p.add_argument("--fecha-desde", default=None)
    p.add_argument("--fecha-hasta", default=None)
    p.add_argument("--sin-empaquetar", action="store_true",
                   help="reusar el tarball que ya esta en S3")
    a = p.parse_args()

    args = ["--limit", str(a.limit), "--reclamo"]
    if a.con_clips:
        args.append("--con-clips")
    if a.fecha_desde:
        args += ["--fecha-desde", a.fecha_desde]
    if a.fecha_hasta:
        args += ["--fecha-hasta", a.fecha_hasta]

    lanzar(a.instancias, args, empaquetar=not a.sin_empaquetar)


if __name__ == "__main__":
    main()
