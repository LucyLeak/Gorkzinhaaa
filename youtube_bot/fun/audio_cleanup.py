"""Audio file cleanup — removes old TTS files, prioritizing larger files first."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from youtube_bot.config import Settings

logger = logging.getLogger(__name__)

# Default thresholds
DEFAULT_MAX_TOTAL_MB = 200  # Keep total audio under 200 MB
DEFAULT_MAX_AGE_HOURS = 24  # Delete files older than 24 hours
DEFAULT_MIN_KEEP = 10       # Always keep at least the 10 most recent files


def _get_audio_files(audio_dir: Path) -> list[tuple[Path, int, float]]:
    """Return list of (path, size_bytes, mtime) for all .mp3 files, sorted by size DESC."""
    files: list[tuple[Path, int, float]] = []
    if not audio_dir.is_dir():
        return files
    for f in audio_dir.glob("*.mp3"):
        if f.is_file():
            stat = f.stat()
            files.append((f, stat.st_size, stat.st_mtime))
    # Sort by size descending (largest first = highest priority for deletion)
    files.sort(key=lambda x: x[1], reverse=True)
    return files


def cleanup_audio_files(
    audio_dir: str | Path,
    max_total_mb: int = DEFAULT_MAX_TOTAL_MB,
    max_age_hours: int = DEFAULT_MAX_AGE_HOURS,
    min_keep: int = DEFAULT_MIN_KEEP,
    dry_run: bool = False,
) -> dict:
    """Delete old/large TTS audio files to free disk space.

    Strategy (in order):
      1. Delete files older than max_age_hours (largest first)
      2. If total size still exceeds max_total_mb, delete largest files
         until under the limit (always keeping at least min_keep files)

    Returns a dict with cleanup statistics.
    """
    import time

    audio_dir = Path(audio_dir)
    files = _get_audio_files(audio_dir)

    if not files:
        return {"deleted": 0, "freed_mb": 0, "remaining_files": 0, "remaining_mb": 0}

    now = time.time()
    max_age_seconds = max_age_hours * 3600
    max_total_bytes = max_total_mb * 1024 * 1024

    deleted_count = 0
    freed_bytes = 0
    to_keep: set[Path] = set()

    # ── Pass 1: Delete old files (largest first) ──────────────────
    for filepath, size, mtime in files:
        age = now - mtime
        if age > max_age_seconds:
            # Don't delete if it would leave us with fewer than min_keep
            remaining = len(files) - deleted_count - 1
            if remaining >= min_keep:
                if dry_run:
                    logger.info("[DRY RUN] Deletaria (antigo): %s (%.1f MB, %.1fh)", filepath.name, size / 1e6, age / 3600)
                else:
                    try:
                        filepath.unlink()
                        logger.info("🗑️ Deletado (antigo): %s (%.1f MB)", filepath.name, size / 1e6)
                    except OSError as exc:
                        logger.warning("Falha ao deletar %s: %s", filepath.name, exc)
                        continue
                deleted_count += 1
                freed_bytes += size
            else:
                to_keep.add(filepath)

    # ── Pass 2: Enforce total size limit (largest first) ──────────
    remaining_files = [f for f in files if f[0] not in to_keep and f[0].exists()]
    total_size = sum(f[1] for f in remaining_files)

    if total_size > max_total_bytes:
        # Sort by size descending again (already sorted, but re-filter)
        for filepath, size, _mtime in remaining_files:
            if total_size <= max_total_bytes:
                break
            # Always keep at least min_keep files
            currently_kept = sum(1 for f in remaining_files if f[0].exists() and f[0] not in to_keep)
            if currently_kept - 1 < min_keep:
                break

            if dry_run:
                logger.info("[DRY RUN] Deletaria (tamanho): %s (%.1f MB)", filepath.name, size / 1e6)
            else:
                try:
                    filepath.unlink()
                    logger.info("🗑️ Deletado (tamanho): %s (%.1f MB)", filepath.name, size / 1e6)
                except OSError as exc:
                    logger.warning("Falha ao deletar %s: %s", filepath.name, exc)
                    continue
            deleted_count += 1
            freed_bytes += size
            total_size -= size

    # ── Final stats ──────────────────────────────────────────────
    final_files = _get_audio_files(audio_dir)
    final_count = len(final_files)
    final_size = sum(f[1] for f in final_files)

    result = {
        "deleted": deleted_count,
        "freed_mb": round(freed_bytes / 1e6, 2),
        "remaining_files": final_count,
        "remaining_mb": round(final_size / 1e6, 2),
        "dry_run": dry_run,
    }
    logger.info("Limpeza de audio: %s", result)
    return result
