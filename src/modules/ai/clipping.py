"""Corte exacto de audio via FFmpeg. Solo ejecuta cortes tecnicos -- no analiza
texto ni decide limites (eso ya lo resolvieron segment_news + map_words_to_time
antes de llegar aqui)."""
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ClipResult:
    output_path: Path
    start_time: float
    end_time: float
    duration: float


def clip_audio(source_path: Path, output_path: Path, start_time: float, end_time: float) -> ClipResult:
    if end_time <= start_time:
        raise ValueError(f"end_time ({end_time}) debe ser mayor que start_time ({start_time})")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", str(source_path),
            "-ss", f"{start_time:.2f}",
            "-to", f"{end_time:.2f}",
            "-acodec", "libmp3lame", "-q:a", "2",
            str(output_path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg fallo (code {result.returncode}): {result.stderr[-2000:]}")

    return ClipResult(
        output_path=output_path,
        start_time=start_time,
        end_time=end_time,
        duration=end_time - start_time,
    )
