"""Prueba end-to-end del pipeline completo, con archivos locales:
words.json (fixture) -> segment_news (OpenAI real) -> map_words_to_time ->
clip_audio (ffmpeg real).

Es una prueba de integracion real (llama a la API de OpenAI y ejecuta ffmpeg
como subproceso) -- se salta automaticamente si no hay OPENAI_API_KEY
configurada o si ffmpeg no esta instalado, para no romper a otros
desarrolladores ni CI sin esos requisitos."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from src.application.orchestrator import MediaProcessingOrchestrator, ProcessAudioJob
from src.infrastructure.config import settings
from src.modules.ai.providers.openai_provider import OpenAIAnalysisProvider

FIXTURES = Path(__file__).parent / "fixtures"

pytestmark = [
    pytest.mark.skipif(settings.openai_api_key is None, reason="requiere OPENAI_API_KEY"),
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="requiere ffmpeg instalado"),
]


@pytest.fixture
def synthetic_audio(tmp_path: Path) -> Path:
    """Genera un tono de la misma duracion que sample_words.json -- no es
    audio real, solo sirve para validar la mecanica de corte (duracion exacta,
    padding aplicado) sin depender de un archivo de radio real."""
    words = json.loads((FIXTURES / "sample_words.json").read_text(encoding="utf-8"))
    duration = words[-1]["end"] + 5
    audio_path = tmp_path / "source.mp3"
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration:.2f}",
            "-c:a", "libmp3lame", str(audio_path),
        ],
        check=True, capture_output=True,
    )
    return audio_path


def test_process_audio_end_to_end(tmp_path: Path, synthetic_audio: Path):
    orchestrator = MediaProcessingOrchestrator(
        ai_provider=OpenAIAnalysisProvider(
            api_key=settings.openai_api_key.get_secret_value(),
            model=settings.openai_model,
        )
    )
    job = ProcessAudioJob(
        words_json_path=FIXTURES / "sample_words.json",
        audio_path=synthetic_audio,
        output_dir=tmp_path / "clips",
    )

    news = orchestrator.process_audio(job)

    # El transcript de la fixture tiene exactamente 2 noticias reales entre
    # relleno/publicidad -- ya validado manualmente en sesiones anteriores.
    assert len(news) == 2

    for item in news:
        assert item.clip.output_path.exists()
        assert item.end_time > item.start_time
        assert item.start_time >= 0.0

        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(item.clip.output_path)],
            capture_output=True, text=True, check=True,
        )
        real_duration = float(probe.stdout.strip())
        expected_duration = item.end_time - item.start_time
        assert abs(real_duration - expected_duration) < 0.1
