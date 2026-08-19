# telegram-pdf-watcher.service

Systemd unit para `scripts/telegram_pdf_watcher.py` en el servidor
(`/home/ubuntu/app`, mismo host y venv que usan los scripts sueltos --
ver `docs/ESTADO_CORRIDA_20260808.md`). Corre fuera de Docker porque
necesita el archivo de sesion de Telethon persistido en disco.

## Instalacion

```bash
# 1. En .env (ya en el servidor) completa las variables TELEGRAM_* --
#    ver .env.example. api_id / api_hash salen de https://my.telegram.org.

# 2. Login interactivo UNA VEZ, a mano (systemd no tiene TTY para esto).
#    Pide telefono + codigo de Telegram y crea telegram_pdf_watcher.session.
cd /home/ubuntu/app
/home/ubuntu/venv/bin/python scripts/telegram_pdf_watcher.py --backfill 0
# Ctrl+C apenas veas "conectado, escuchando @..." -- ya quedo autenticado.

# 3. Instalar y habilitar el servicio.
sudo cp deploy/systemd/telegram-pdf-watcher.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-pdf-watcher

# 4. Verificar.
sudo systemctl status telegram-pdf-watcher
tail -f /home/ubuntu/telegram_pdf_watcher.log
```

## Notas

- Los PDF quedan en `TELEGRAM_PDF_DOWNLOAD_DIR` (default
  `./data/telegram_pdfs/<fecha>/`), dentro del repo -- no se suben a S3 ni se
  registran en Postgres (alcance actual: solo descarga).
- Si la sesion expira o se revoca desde Telegram, el servicio va a loguear el
  error y reintentar cada 15s sin poder autenticarse -- repetir el paso 2.
- `journalctl -u telegram-pdf-watcher -f` tambien sirve si prefieres el log
  de systemd en vez del archivo.
