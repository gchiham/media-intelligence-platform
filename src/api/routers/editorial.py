"""Endpoints de News (Editorial). Capa de transporte pura: valida el request,
resuelve dependencias, llama a NoticiaService, devuelve la respuesta. Toda la
logica de negocio vive en NoticiaService -- ver docs/EDITORIAL_DOMAIN.md y
docs/API.md.

Excepcion: GET /pending no pasa por NoticiaService porque no es una operacion
de negocio (no muta nada, no aplica ninguna regla) -- es una lectura directa
via NoticiaRepository. NoticiaService.siguiente_pendiente_con_lock() no sirve
para esto: usa SELECT FOR UPDATE y bloquearia filas solo por listarlas.
"""
import html
import json
import uuid
from datetime import datetime

import boto3
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse

from src.api.deps import (
    get_noticia_repository,
    get_noticia_service,
    get_noticia_version_repository,
)
from src.api.schemas.editorial import (
    ApproveRequest,
    NewsResponse,
    NewsSummaryResponse,
    NewsVersionResponse,
    RejectRequest,
    SaveDraftRequest,
    StartReviewRequest,
)
from src.infrastructure.config import settings
from src.modules.editorial.models import Noticia
from src.modules.editorial.repositories import NoticiaRepository, NoticiaVersionRepository
from src.modules.editorial.search import RateLimitExceeded, expandir_query
from src.modules.editorial.services import NoticiaService

router = APIRouter(prefix="/news", tags=["news"])

_ESTADO_COLORS = {
    "pendiente": "#9ca3af",
    "en_revision": "#f59e0b",
    "aprobada": "#10b981",
    "rechazada": "#ef4444",
    "publicada": "#3b82f6",
}


def _dashboard_row_html(row: dict, token: str) -> str:
    titulo = html.escape(row["titulo"] or "(sin titulo)")
    resumen = html.escape(row["resumen"] or "")
    transcripcion = html.escape(row["transcripcion_texto"] or "(sin transcripcion)")
    estado = row["estado"] or ""
    color = _ESTADO_COLORS.get(estado, "#9ca3af")
    created = row["created_at"]
    created_str = created.strftime("%Y-%m-%d %H:%M:%S") if created else "-"
    medio = html.escape(row["medio_nombre"] or "-")
    programa = html.escape(row["programa_nombre"] or "-")
    prioridad = html.escape(row["prioridad"] or "-")
    ai_score = row["ai_score"] if row["ai_score"] is not None else "-"

    meta = row["metadatos_ia"] or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (json.JSONDecodeError, TypeError):
            meta = {}
    keywords = meta.get("keywords") or []
    keywords_html = "".join(f'<span class="tag">{html.escape(str(k))}</span>' for k in keywords[:8])

    player_html = ""
    if row["clip_s3_uri"]:
        clip_url = html.escape(f"/api/v1/news/{row['id']}/clip?token={token}")
        player_html = f'<audio class="player" controls preload="none" src="{clip_url}"></audio>'

    return f"""
    <details class="row" data-id="{row["id"]}" data-medio="{html.escape(row["medio_nombre"] or "-")}">
      <summary>
        <span class="badge" style="background:{color}">{html.escape(estado)}</span>
        <span class="titulo">{titulo}</span>
        <span class="date">{created_str}</span>
      </summary>
      <div class="row-body">
        <p class="meta">{medio} &middot; {programa} &middot; prioridad: {prioridad} &middot; ai_score: {ai_score}</p>
        {player_html}
        <p class="resumen"><strong>Resumen:</strong> {resumen}</p>
        <div class="tags">{keywords_html}</div>
        <p class="transcripcion-label">Transcripcion completa:</p>
        <pre class="transcripcion">{transcripcion}</pre>
      </div>
    </details>
    """


def _dashboard_tabs_html(rows: list[dict]) -> str:
    counts: dict[str, int] = {}
    for row in rows:
        medio = row["medio_nombre"] or "-"
        counts[medio] = counts.get(medio, 0) + 1

    tabs = [f'<button class="tab active" data-medio="__all__">Todas ({len(rows)})</button>']
    for medio in sorted(counts):
        medio_esc = html.escape(medio)
        tabs.append(f'<button class="tab" data-medio="{medio_esc}">{medio_esc} ({counts[medio]})</button>')
    return "\n".join(tabs)


_DASHBOARD_PAGE = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<title>Dashboard de Noticias</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, Segoe UI, Roboto, sans-serif;
    max-width: 820px;
    margin: 0 auto;
    padding: 20px 16px 60px;
    background: #0b0f14;
    color: #e5e7eb;
  }}
  h1 {{ font-size: 1.3rem; margin: 0 0 2px; }}
  .subtitle {{ color: #6b7280; font-size: 0.8rem; margin: 0 0 16px; }}
  .search {{
    position: sticky;
    top: 0;
    z-index: 2;
    display: flex;
    gap: 8px;
    padding: 10px 0 0;
    background: #0b0f14;
  }}
  .search input {{
    flex: 1;
    background: #131922;
    border: 1px solid #232b36;
    color: #e5e7eb;
    font-size: 0.9rem;
    padding: 8px 12px;
    border-radius: 8px;
    outline: none;
  }}
  .search input:focus {{ border-color: #3b4454; }}
  .search button {{
    background: #3b4454;
    color: #e5e7eb;
    border: none;
    font-size: 0.85rem;
    padding: 8px 16px;
    border-radius: 8px;
    cursor: pointer;
  }}
  .search button:hover {{ background: #4b5568; }}
  .search .clear {{ background: transparent; border: 1px solid #232b36; color: #9ca3af; display: none; }}
  .search-status {{ color: #6b7280; font-size: 0.78rem; margin: 8px 0 0; min-height: 1.1em; }}
  .tabs {{
    position: sticky;
    top: 0;
    z-index: 1;
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    padding: 10px 0;
    margin-bottom: 10px;
    background: #0b0f14;
    border-bottom: 1px solid #232b36;
  }}
  .tab {{
    background: #131922;
    border: 1px solid #232b36;
    color: #9ca3af;
    font-size: 0.78rem;
    padding: 5px 12px;
    border-radius: 999px;
    cursor: pointer;
    white-space: nowrap;
  }}
  .tab:hover {{ background: #182130; }}
  .tab.active {{ background: #3b4454; color: #e5e7eb; border-color: #3b4454; }}
  .row.hidden {{ display: none; }}
  .row {{
    background: #131922;
    border: 1px solid #232b36;
    border-radius: 8px;
    margin-bottom: 6px;
    overflow: hidden;
  }}
  .row[open] {{ border-color: #3b4454; }}
  .row summary {{
    list-style: none;
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 8px 12px;
    font-size: 0.85rem;
  }}
  .row summary::-webkit-details-marker {{ display: none; }}
  .row summary:hover {{ background: #182130; }}
  .badge {{
    flex: none;
    color: #0b0f14;
    font-weight: 600;
    font-size: 0.65rem;
    padding: 2px 8px;
    border-radius: 999px;
    text-transform: uppercase;
  }}
  .titulo {{
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }}
  .date {{ flex: none; color: #6b7280; font-size: 0.75rem; }}
  .row-body {{ padding: 4px 14px 14px; border-top: 1px solid #1c2532; }}
  .meta {{ color: #9ca3af; font-size: 0.8rem; margin: 10px 0 8px; }}
  .resumen {{ font-size: 0.9rem; line-height: 1.4; color: #d1d5db; margin: 0 0 8px; }}
  .tags {{ margin-bottom: 12px; }}
  .tag {{
    display: inline-block;
    background: #1f2937;
    color: #9ca3af;
    font-size: 0.7rem;
    padding: 2px 8px;
    border-radius: 6px;
    margin-right: 6px;
    margin-bottom: 4px;
  }}
  .transcripcion-label {{ color: #6b7280; font-size: 0.75rem; text-transform: uppercase; margin: 0 0 4px; }}
  .transcripcion {{
    white-space: pre-wrap;
    word-wrap: break-word;
    font-family: inherit;
    font-size: 0.85rem;
    line-height: 1.5;
    color: #c7cdd6;
    background: #0e131a;
    border: 1px solid #1c2532;
    border-radius: 6px;
    padding: 10px 12px;
    max-height: 400px;
    overflow-y: auto;
    margin: 0;
  }}
  .count {{ color: #6b7280; }}
  .player {{ width: 100%; height: 32px; margin: 6px 0 10px; }}
</style>
</head>
<body>
  <h1>Dashboard de Noticias</h1>
  <p class="subtitle">Generado {generated_at} &middot; <span class="count">{count} noticias</span> &middot; mas reciente primero &middot; click en una fila para ver resumen + transcripcion completa</p>
  <div class="search">
    <input type="text" id="search-input" placeholder="Buscar por palabra clave..." />
    <button id="search-btn">Buscar</button>
    <button id="search-clear" class="clear">Limpiar</button>
  </div>
  <p class="search-status" id="search-status"></p>
  <div class="tabs" id="tabs">
    {tabs}
  </div>
  <div id="rows">
    {cards}
  </div>
  <script>
    (function() {{
      var tabs = document.querySelectorAll('.tab');
      var rowsContainer = document.getElementById('rows');
      var rows = Array.prototype.slice.call(document.querySelectorAll('.row'));
      var originalOrder = rows.slice();
      var token = new URLSearchParams(window.location.search).get('token');
      var searchInput = document.getElementById('search-input');
      var searchBtn = document.getElementById('search-btn');
      var searchClear = document.getElementById('search-clear');
      var searchStatus = document.getElementById('search-status');
      var tabsBar = document.getElementById('tabs');

      tabs.forEach(function(tab) {{
        tab.addEventListener('click', function() {{
          tabs.forEach(function(t) {{ t.classList.remove('active'); }});
          tab.classList.add('active');
          var medio = tab.getAttribute('data-medio');
          rows.forEach(function(row) {{
            var show = medio === '__all__' || row.getAttribute('data-medio') === medio;
            row.classList.toggle('hidden', !show);
          }});
        }});
      }});

      function runSearch() {{
        var q = searchInput.value.trim();
        if (!q) return;
        searchStatus.textContent = 'Buscando...';
        searchBtn.disabled = true;
        fetch('/api/v1/news/search?token=' + encodeURIComponent(token) + '&q=' + encodeURIComponent(q))
          .then(function(r) {{
            if (!r.ok) throw new Error('HTTP ' + r.status);
            return r.json();
          }})
          .then(function(data) {{
            var ids = {{}};
            data.results.forEach(function(r) {{ ids[r.id] = true; }});
            rows.forEach(function(row) {{
              row.classList.toggle('hidden', !ids[row.getAttribute('data-id')]);
            }});
            tabsBar.style.display = 'none';
            searchClear.style.display = 'inline-block';
            var suffix = data.expandido ? ' (busqueda ampliada con variantes/alias)' : '';
            searchStatus.textContent = data.count + ' resultado(s) para "' + data.query + '"' + suffix;
          }})
          .catch(function(err) {{
            searchStatus.textContent = 'Error buscando: ' + err.message;
          }})
          .finally(function() {{ searchBtn.disabled = false; }});
      }}

      function clearSearch() {{
        searchInput.value = '';
        searchStatus.textContent = '';
        originalOrder.forEach(function(el) {{ rowsContainer.appendChild(el); }});
        tabsBar.style.display = '';
        searchClear.style.display = 'none';
        var activeTab = document.querySelector('.tab.active') || tabs[0];
        activeTab.click();
      }}

      searchBtn.addEventListener('click', runSearch);
      searchClear.addEventListener('click', clearSearch);
      searchInput.addEventListener('keydown', function(e) {{
        if (e.key === 'Enter') runSearch();
      }});
    }})();
  </script>
</body>
</html>
"""


def _to_news_response(noticia: Noticia, versiones: NoticiaVersionRepository) -> NewsResponse:
    version = versiones.get_by_id(noticia.version_actual_id) if noticia.version_actual_id else None
    return NewsResponse(
        id=noticia.id,
        status=noticia.estado.value,
        assigned_to=noticia.asignado_a,
        clip_start_seconds=noticia.clip_inicio_seg,
        clip_end_seconds=noticia.clip_fin_seg,
        title=version.titulo if version else None,
        summary=version.resumen if version else None,
    )


@router.get(
    "/dashboard",
    response_class=HTMLResponse,
    status_code=status.HTTP_200_OK,
    summary="Dashboard temporal de todas las noticias",
    description=(
        "Pagina HTML de solo lectura con todas las noticias (cualquier estado), mas "
        "reciente primero. Protegida por `token` en la query string (no es auth de "
        "usuario -- ver `DASHBOARD_TOKEN` en `.env`); responde 404 si no coincide, "
        "para no filtrar si el endpoint existe."
    ),
)
def news_dashboard(
    token: str,
    noticias: NoticiaRepository = Depends(get_noticia_repository),
) -> HTMLResponse:
    if not settings.dashboard_token or token != settings.dashboard_token:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    rows = noticias.listar_todas_con_detalle()
    cards = "\n".join(_dashboard_row_html(row, token) for row in rows)
    page = _DASHBOARD_PAGE.format(
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        count=len(rows),
        tabs=_dashboard_tabs_html(rows),
        cards=cards,
    )
    return HTMLResponse(content=page)


_EXPAND_THRESHOLD = 3  # si el ILIKE plano ya trae esto o mas, no se llama al LLM


@router.get(
    "/search",
    status_code=status.HTTP_200_OK,
    summary="Busqueda de noticias por keywords, con expansion por LLM cuando hace falta",
    description=(
        "Busqueda por texto (ILIKE) sobre titulo/resumen/transcripcion/keywords/medio. "
        "Si el match directo trae pocos resultados, se llama a Claude UNA vez (solo con "
        "la query, nunca con las noticias) para expandir a alias/variantes/errores "
        "ortograficos/terminos institucionales relacionados, y se reintenta -- la "
        "mayoria de busquedas comunes nunca llegan a esto. Cacheado por query (1h) y "
        "con rate limit. Protegida por `token`."
    ),
)
def news_search(
    token: str,
    q: str,
    noticias: NoticiaRepository = Depends(get_noticia_repository),
) -> dict:
    if not settings.dashboard_token or token != settings.dashboard_token:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    q = q.strip()
    if not q:
        return {"query": q, "count": 0, "results": []}

    terminos = [t for t in q.split() if len(t) >= 2][:8]
    candidatos = noticias.buscar_candidatos(terminos, limit=300)
    expandido = False

    if len(candidatos) < _EXPAND_THRESHOLD and settings.anthropic_api_key:
        try:
            extra_terminos = expandir_query(
                q, settings.anthropic_api_key.get_secret_value(), settings.anthropic_model
            )
        except RateLimitExceeded:
            extra_terminos = []
        extra_terminos = [t for t in extra_terminos if t.lower() not in [x.lower() for x in terminos]]
        if extra_terminos:
            expandido = True
            existentes = {c["id"] for c in candidatos}
            extra_candidatos = noticias.buscar_candidatos(extra_terminos, limit=300)
            candidatos += [c for c in extra_candidatos if c["id"] not in existentes]

    results = [{"id": str(c["id"])} for c in candidatos]
    return {"query": q, "count": len(results), "results": results, "expandido": expandido}


@router.get(
    "/{news_id}/clip",
    include_in_schema=False,
)
def news_clip(
    news_id: uuid.UUID,
    token: str,
    noticias: NoticiaRepository = Depends(get_noticia_repository),
) -> RedirectResponse:
    """Redirige (307) a una URL presignada de S3 para el clip de audio de
    esta noticia -- el bucket es privado, asi que <audio src> no puede
    apuntar directo a el. Misma proteccion por token que /dashboard, del
    que este endpoint es soporte (no se usa suelto)."""
    if not settings.dashboard_token or token != settings.dashboard_token:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    noticia = noticias.get_by_id(news_id)
    if noticia is None or not noticia.clip_s3_uri:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    bucket, _, key = noticia.clip_s3_uri.removeprefix("s3://").partition("/")
    url = boto3.client("s3", region_name=settings.aws_region).generate_presigned_url(
        "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=3600
    )
    return RedirectResponse(url)


@router.get(
    "/pending",
    response_model=list[NewsSummaryResponse],
    status_code=status.HTTP_200_OK,
    summary="Listar noticias pendientes",
    description="Lista, en orden FIFO, las noticias en estado PENDIENTE (sin bloquear ninguna).",
)
def list_pending_news(
    noticias: NoticiaRepository = Depends(get_noticia_repository),
    versiones: NoticiaVersionRepository = Depends(get_noticia_version_repository),
) -> list[NewsSummaryResponse]:
    pendientes = noticias.listar_pendientes()
    resultado = []
    for noticia in pendientes:
        version = versiones.get_by_id(noticia.version_actual_id) if noticia.version_actual_id else None
        resultado.append(
            NewsSummaryResponse(
                id=noticia.id,
                status=noticia.estado.value,
                title=version.titulo if version else None,
                created_at=noticia.created_at,
            )
        )
    return resultado


@router.post(
    "/start-review",
    response_model=NewsResponse,
    status_code=status.HTTP_200_OK,
    summary="Tomar la siguiente noticia de la cola",
    description=(
        "Toma la noticia PENDIENTE mas antigua de la cola FIFO, la bloquea y la asigna al "
        "editor. Si la cola esta vacia, responde 204 No Content."
    ),
)
def start_review(
    body: StartReviewRequest,
    service: NoticiaService = Depends(get_noticia_service),
    versiones: NoticiaVersionRepository = Depends(get_noticia_version_repository),
) -> NewsResponse:
    noticia = service.start_review(body.editor_id)
    return _to_news_response(noticia, versiones)


@router.post(
    "/{news_id}/draft",
    response_model=NewsVersionResponse,
    status_code=status.HTTP_200_OK,
    summary="Guardar un borrador (nueva version)",
    description=(
        "Crea una NoticiaVersion nueva con los campos editados -- nunca sobrescribe la "
        "version anterior. Requiere que `editor_id` tenga la noticia bloqueada (EN_REVISION "
        "y asignada a el)."
    ),
)
def save_draft(
    news_id: uuid.UUID,
    body: SaveDraftRequest,
    service: NoticiaService = Depends(get_noticia_service),
) -> NewsVersionResponse:
    version = service.save_draft(
        news_id,
        body.editor_id,
        titulo=body.title,
        resumen=body.summary,
        transcripcion_texto=body.transcription_text,
    )
    return NewsVersionResponse(
        news_id=version.noticia_id,
        version_number=version.numero_version,
        title=version.titulo,
        summary=version.resumen,
        transcription_text=version.transcripcion_texto,
        is_ai_generated=version.es_generada_por_ia,
        edited_by=version.editado_por,
        created_at=version.created_at,
    )


@router.post(
    "/{news_id}/approve",
    response_model=NewsResponse,
    status_code=status.HTTP_200_OK,
    summary="Aprobar una noticia",
    description=(
        "Aprueba la noticia completa (FR-056, una sola accion) y libera el bloqueo. "
        "Requiere que `editor_id` tenga la noticia bloqueada. No publica al cliente "
        "todavia -- fuera de alcance de esta fase."
    ),
)
def approve_news(
    news_id: uuid.UUID,
    body: ApproveRequest,
    service: NoticiaService = Depends(get_noticia_service),
    versiones: NoticiaVersionRepository = Depends(get_noticia_version_repository),
) -> NewsResponse:
    noticia = service.approve(news_id, body.editor_id)
    return _to_news_response(noticia, versiones)


@router.post(
    "/{news_id}/reject",
    response_model=NewsResponse,
    status_code=status.HTTP_200_OK,
    summary="Rechazar una noticia",
    description="Rechaza la noticia con un motivo opcional, y libera el bloqueo.",
)
def reject_news(
    news_id: uuid.UUID,
    body: RejectRequest,
    service: NoticiaService = Depends(get_noticia_service),
    versiones: NoticiaVersionRepository = Depends(get_noticia_version_repository),
) -> NewsResponse:
    noticia = service.reject(news_id, body.editor_id, motivo=body.reason)
    return _to_news_response(noticia, versiones)
