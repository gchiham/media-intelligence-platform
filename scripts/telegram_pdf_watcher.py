"""Userbot: escucha un canal publico de Telegram y descarga los PDF que publique.

Por que un userbot y no uno de nuestros bots (ChihamBot, hncomprasbot): el
Bot API solo entrega `channel_post` a un bot si ese bot es administrador del
canal. Canales de terceros (ej. Hondurasperiodico) no nos van a dar admin,
asi que la unica forma de "escuchar" es loguearse con una cuenta normal de
Telegram (via Telethon) y unirse como cualquier suscriptor.

No toca la base de datos ni el pipeline de Noticia/Medio -- solo guarda cada
PDF nuevo en TELEGRAM_PDF_DOWNLOAD_DIR. Corre en vivo (event handler), no es
polling; dejalo corriendo como servicio (systemd, pm2, screen, etc).

Primera corrida: pide telefono + codigo de Telegram por consola (login
interactivo) y guarda la sesion en `<TELEGRAM_SESSION_NAME>.session`.
Corridas siguientes reusan esa sesion sin pedir nada.

Uso:
    python scripts/telegram_pdf_watcher.py
    python scripts/telegram_pdf_watcher.py --backfill 50   # baja los ultimos N PDF antes de escuchar
"""
import argparse
import asyncio
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv  # noqa: E402
from telethon import TelegramClient, events  # noqa: E402

load_dotenv()


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}  {msg}", flush=True)


def slug(name: str) -> str:
    """Nombre de archivo seguro; conserva el nombre original del PDF cuando existe."""
    name = re.sub(r"[^\w.\-]+", "_", name).strip("_")
    return name or "documento.pdf"


async def guardar_pdf(client: TelegramClient, message, destino: Path) -> None:
    doc = message.document
    nombre_original = next(
        (a.file_name for a in doc.attributes if getattr(a, "file_name", None)),
        f"{message.chat_id}_{message.id}.pdf",
    )
    fecha = message.date.astimezone(timezone.utc).strftime("%Y-%m-%d")
    ruta = destino / fecha / f"{message.id}_{slug(nombre_original)}"
    if ruta.exists():
        log(f"ya existe, salto: {ruta}")
        return
    ruta.parent.mkdir(parents=True, exist_ok=True)
    tmp = ruta.with_suffix(ruta.suffix + ".part")
    await client.download_media(message, file=str(tmp))
    tmp.rename(ruta)
    log(f"descargado: {ruta}")


def es_pdf(message) -> bool:
    doc = getattr(message, "document", None)
    if doc is None:
        return False
    if doc.mime_type == "application/pdf":
        return True
    return any(getattr(a, "file_name", "").lower().endswith(".pdf") for a in doc.attributes)


async def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--backfill",
        type=int,
        default=0,
        help="al arrancar, baja los ultimos N PDF ya publicados en el canal (0 = ninguno)",
    )
    args = p.parse_args()

    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        sys.exit("Falta TELEGRAM_API_ID / TELEGRAM_API_HASH en .env (sacalos en my.telegram.org).")

    session = os.environ.get("TELEGRAM_SESSION_NAME", "telegram_pdf_watcher")
    canal = os.environ.get("TELEGRAM_PDF_CHANNEL", "Hondurasperiodico").rsplit("/", 1)[-1].lstrip("@")
    destino = Path(os.environ.get("TELEGRAM_PDF_DOWNLOAD_DIR", "./data/telegram_pdfs"))
    destino.mkdir(parents=True, exist_ok=True)

    client = TelegramClient(session, int(api_id), api_hash)
    await client.start()  # primera vez: pide telefono + codigo por consola
    entidad = await client.get_entity(canal)
    log(f"conectado, escuchando @{canal} -> {destino}")

    if args.backfill:
        vistos = 0
        async for message in client.iter_messages(entidad, limit=args.backfill * 5):
            if es_pdf(message):
                await guardar_pdf(client, message, destino)
                vistos += 1
                if vistos >= args.backfill:
                    break
        log(f"backfill listo: {vistos} PDF")

    @client.on(events.NewMessage(chats=entidad))
    async def on_new_message(event):
        if es_pdf(event.message):
            await guardar_pdf(client, event.message, destino)

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
