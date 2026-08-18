"""A/B: mismo transcript real, dos modelos de Anthropic (Sonnet 5 vs Haiku 4.5).
Corre dentro del contenedor backend (tiene ANTHROPIC_API_KEY y acceso a Postgres)."""
import json
import time

from sqlalchemy.orm import Session
from sqlalchemy import text

from src.infrastructure.db.engine import get_engine
from src.infrastructure.config import settings
from src.modules.ai.providers.anthropic_provider import AnthropicAnalysisProvider
from src.modules.ai.schemas import Word

SAMPLE_IDS = [
    "019f82af-2481-71ad-9b04-882bdc03341c",  # xy_sps 2026-07-16T14Z
    "019f82af-23a9-74d8-8a9e-19bc5a0ec77d",  # radio_america 2026-07-16T12Z
    "019f82ae-40fb-70f0-91d3-cd8c2beb00c8",  # hch_radio 2026-07-16T11Z
    "019f82b4-a9a1-779f-883a-d62ac441c1e4",  # canal_11 2026-07-16T06Z
    "019f82b4-a68e-7d42-8bfc-ce262248ceeb",  # xy_hrn 2026-07-16T04Z
    "019f82aa-9093-7b3a-b501-c522273fac1f",  # hch_radio 2026-07-16T08Z
]

MODELS = {
    "sonnet-5": "claude-sonnet-5",
    "haiku-4.5": "claude-haiku-4-5-20251001",
}

api_key = settings.anthropic_api_key.get_secret_value()

with Session(get_engine()) as s:
    results = {}
    for tid in SAMPLE_IDS:
        row = s.execute(
            text(
                """
                SELECT t.segmentos, g.s3_key
                FROM transcripciones t JOIN grabaciones g ON g.id = t.grabacion_id
                WHERE t.id = :id
                """
            ),
            {"id": tid},
        ).first()
        words = [Word.model_validate(w) for w in row.segmentos["words"]]
        results[tid] = {"s3_key": row.s3_key, "num_words": len(words), "words": words}

for label, model in MODELS.items():
    print(f"\n{'=' * 70}\nMODELO: {label} ({model})\n{'=' * 70}")
    provider = AnthropicAnalysisProvider(api_key=api_key, model=model)
    total_elapsed = 0.0
    total_segments = 0
    for tid in SAMPLE_IDS:
        entry = results[tid]
        t0 = time.time()
        segments = provider.segment_news(entry["words"])
        elapsed = time.time() - t0
        total_elapsed += elapsed
        total_segments += len(segments)
        print(f"\n--- {entry['s3_key']} ({entry['num_words']} palabras, {elapsed:.1f}s) ---")
        print(f"noticias detectadas: {len(segments)}")
        for seg in segments:
            print(
                f"  [{seg.news_type.value:15s} conf={seg.confidence:.2f}] {seg.title}"
                f" | kw={len(seg.keywords)} people={seg.people} orgs={seg.organizations} loc={seg.locations}"
            )
    results[label + "_summary"] = {"total_elapsed": total_elapsed, "total_segments": total_segments}
    print(f"\n>>> TOTAL {label}: {total_segments} noticias, {total_elapsed:.1f}s")
