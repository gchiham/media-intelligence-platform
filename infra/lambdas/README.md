# Lambdas del cron de chepita

Fuente de las dos funciones Lambda que operan el fleet de transcripción por
horario. **Están desplegadas a mano** (igual que el resto de la infra —
`docs/INFRASTRUCTURE.md` explica que no hay Terraform/CDK todavía); esta carpeta
existe para que el código no viva únicamente dentro de AWS, donde no se puede
revisar ni versionar.

Cuenta `050871635829`, región `us-east-1`.

## Las dos funciones

| Función | Disparador | Qué hace |
|---|---|---|
| `media-intel-chepita-launch` | 4 reglas EventBridge: 10am, 3pm, 8pm, 11pm GMT-6 | Lanza 1 `g6.xlarge` desde el AMI `CHEPITA-L4-v1.2.0` y arranca sus 3 workers por SSM. |
| `media-intel-chepita-supervisor` | `rate(5 minutes)` | Cola vacía → apaga las instancias del cron que pasaron la gracia. Cola con trabajo → les relanza los workers si murieron. |

Rol de ejecución: `media-intel-cron-chepita-lambda` (política en
`iam_policy.json`, trust en `iam_trust_policy.json`).

## Por qué el supervisor hace las dos cosas

`worker_prefetch.py` **sale solo cuando ve la cola vacía** — es deliberado: deja
la instancia lista para apagarse en vez de quemar GPU esperando. El problema es
la ventana entre que los workers mueren y que la instancia se apaga: si entra
trabajo nuevo ahí, la instancia queda encendida y cobrando sin nadie
consumiendo. Pasó el 2026-08-18 (15 grabaciones en cola, 0 workers vivos, la
instancia arriba).

Se resolvió del lado del Lambda y no con un demonio dentro de la instancia
porque así no se agrega una pieza nueva que a su vez pueda morirse, y de paso
cubre el caso en que el propio `launch` falle al arrancar los workers.

**El comando de reinicio es idempotente** (`pgrep` decide) y solo se manda a
instancias con más de 6 minutos de vida. Las dos cosas importan: arrancar
workers de más es peor que el bug original, porque 6 workers en una L4 dan
`CUDA out of memory` (ver `docs/INFRASTRUCTURE.md`, notas del AMI v1.2.0).

## Redesplegar

```bash
cd infra/lambdas/chepita_supervisor
zip -j function.zip lambda_function.py
aws lambda update-function-code \
  --function-name media-intel-chepita-supervisor \
  --zip-file fileb://function.zip
```

**Ojo con los finales de línea.** El supervisor manda a la instancia un script
de shell embebido como string de Python: si el `.py` se guarda con CRLF, el
script llega con `\r` y falla con `syntax error near unexpected token`. Guardar
siempre con LF.

## Lo que quedó pendiente

- La regla de EventBridge sigue llamándose `media-intel-chepita-terminate-check`
  aunque ahora apunta al supervisor, que hace más que terminar. Renombrarla
  implica recrearla y volver a dar permisos.
- El reinicio tarda hasta 5 minutos en detectarse: es la resolución del
  `rate(5 minutes)`.
