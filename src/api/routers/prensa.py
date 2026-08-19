"""Seccion Prensa Digital del portal.

Pagina aparte y no una pestaña mas del dashboard de noticias: un Articulo tiene
otra forma que una Noticia -- no tiene estado editorial, ni clip de audio, ni
transcripcion, y en cambio tiene una URL externa que es lo que el usuario quiere
abrir. Meterlos en las mismas tarjetas obligaba a que la mitad de los controles
no hicieran nada.

Mismo esquema de proteccion que GET /news/dashboard: token compartido en la
query string, 404 si no coincide (no 401/403, para no confirmar que existe).
"""
import html
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse

from src.api.deps import get_articulo_repository
from src.infrastructure.config import settings
from src.modules.prensa.repositories import ArticuloRepository

router = APIRouter(prefix="/prensa", tags=["prensa"])

# Igual que el dashboard de noticias: Honduras es UTC-6 sin horario de verano,
# asi que un offset fijo alcanza y evita depender de tzdata en la imagen.
GMT6 = timezone(timedelta(hours=-6))


def _card_html(row: dict) -> str:
    medio = html.escape(row["medio_nombre"] or "-")
    titulo = html.escape(row["titulo"] or "(sin titulo)")
    url = html.escape(row["url"] or "", quote=True)
    fecha = row["publicado_at"].astimezone(GMT6).strftime("%Y-%m-%d %H:%M")
    autor = f' &middot; {html.escape(row["autor"])}' if row.get("autor") else ""
    # El resumen viene con HTML crudo del feed (la ingesta no lo limpia a
    # proposito): se escapa entero y se recorta, no se interpreta.
    resumen = html.escape(row["resumen"] or "")[:400]
    cuerpo = "" if row["tiene_cuerpo"] else '<span class="parcial">solo titular</span>'
    return f"""
    <article class="card" data-medio="{medio}" data-buscar="{medio.lower()} {titulo.lower()}">
      <div class="meta"><span class="medio">{medio}</span> &middot; {fecha}{autor} {cuerpo}</div>
      <h2><a href="{url}" target="_blank" rel="noopener noreferrer">{titulo}</a></h2>
      <p class="resumen">{resumen}</p>
    </article>"""


def _tabs_html(rows: list[dict]) -> str:
    counts: dict[str, int] = {}
    for row in rows:
        medio = row["medio_nombre"] or "-"
        counts[medio] = counts.get(medio, 0) + 1
    tabs = [f'<button class="tab active" data-medio="__all__">Todos ({len(rows)})</button>']
    for medio in sorted(counts):
        esc = html.escape(medio)
        tabs.append(f'<button class="tab" data-medio="{esc}">{esc} ({counts[medio]})</button>')
    return "\n".join(tabs)


_PAGE = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<title>Prensa Digital</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; max-width: 820px;
         margin: 0 auto; padding: 20px 16px 60px; background: #0b0f14; color: #e5e7eb; }}
  h1 {{ font-size: 1.3rem; margin: 0 0 2px; }}
  .subtitle {{ color: #6b7280; font-size: 0.8rem; margin: 0 0 12px; }}
  .subtitle a {{ color: #6b7280; }}
  .search {{ position: sticky; top: 0; z-index: 2; padding: 10px 0 0; background: #0b0f14; }}
  .search input {{ width: 100%; background: #131922; border: 1px solid #232b36; color: #e5e7eb;
                   font-size: 0.9rem; padding: 8px 12px; border-radius: 8px; outline: none; }}
  .tabs {{ display: flex; flex-wrap: wrap; gap: 6px; margin: 10px 0 14px; }}
  .tab {{ background: #131922; border: 1px solid #232b36; color: #9ca3af; font-size: 0.75rem;
          padding: 4px 10px; border-radius: 999px; cursor: pointer; }}
  .tab.active {{ background: #3b4454; color: #e5e7eb; }}
  .card {{ border: 1px solid #1b222c; border-radius: 10px; padding: 12px 14px; margin-bottom: 10px;
           background: #10151c; }}
  .card.oculta {{ display: none; }}
  .meta {{ color: #6b7280; font-size: 0.72rem; margin-bottom: 4px; }}
  .medio {{ color: #93c5fd; }}
  .parcial {{ color: #f59e0b; border: 1px solid #422f10; border-radius: 4px;
              padding: 0 4px; font-size: 0.65rem; margin-left: 4px; }}
  .card h2 {{ font-size: 0.95rem; margin: 0 0 6px; font-weight: 600; line-height: 1.35; }}
  .card h2 a {{ color: #e5e7eb; text-decoration: none; }}
  .card h2 a:hover {{ text-decoration: underline; }}
  .resumen {{ color: #9ca3af; font-size: 0.82rem; margin: 0; line-height: 1.45; }}
  .vacio {{ color: #6b7280; font-size: 0.85rem; }}
</style>
</head>
<body>
<h1>Prensa Digital</h1>
<p class="subtitle">{count} articulos &middot; generado {generated_at} (GMT-6) &middot;
   <a href="/api/v1/news/dashboard?token={token}">ir a noticias de radio y TV</a></p>
<div class="search"><input id="q" type="search" placeholder="Filtrar por medio o titular..."></div>
<div class="tabs">{tabs}</div>
<div id="lista">{cards}</div>
<p class="vacio" id="vacio" hidden>Sin resultados.</p>
<script>
  const cards = Array.from(document.querySelectorAll('.card'));
  let medio = '__all__';
  const q = document.getElementById('q');
  function aplicar() {{
    const texto = q.value.trim().toLowerCase();
    let visibles = 0;
    for (const card of cards) {{
      const okMedio = medio === '__all__' || card.dataset.medio === medio;
      const okTexto = !texto || card.dataset.buscar.includes(texto);
      const mostrar = okMedio && okTexto;
      card.classList.toggle('oculta', !mostrar);
      if (mostrar) visibles++;
    }}
    document.getElementById('vacio').hidden = visibles > 0;
  }}
  q.addEventListener('input', aplicar);
  document.querySelectorAll('.tab').forEach(tab => tab.addEventListener('click', () => {{
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    tab.classList.add('active');
    medio = tab.dataset.medio;
    aplicar();
  }}));
</script>
</body>
</html>"""


def render_pagina(rows: list[dict], token: str) -> str:
    return _PAGE.format(
        count=len(rows),
        generated_at=datetime.now(GMT6).strftime("%Y-%m-%d %H:%M:%S"),
        token=html.escape(token, quote=True),
        tabs=_tabs_html(rows),
        cards="\n".join(_card_html(row) for row in rows),
    )


@router.get(
    "/dashboard",
    response_class=HTMLResponse,
    status_code=status.HTTP_200_OK,
    summary="Seccion Prensa Digital: articulos ingeridos por RSS/sitemap",
    description=(
        "Pagina HTML de solo lectura con los articulos publicados por los medios "
        "web monitoreados, mas reciente primero, con pestañas por medio. Protegida "
        "por `token` en la query string (mismo `DASHBOARD_TOKEN` que "
        "`/news/dashboard`); responde 404 si no coincide."
    ),
)
def prensa_dashboard(
    token: str,
    limite: int = 500,
    articulos: ArticuloRepository = Depends(get_articulo_repository),
) -> HTMLResponse:
    if not settings.dashboard_token or token != settings.dashboard_token:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    return HTMLResponse(content=render_pagina(articulos.listar_recientes(limite), token))
