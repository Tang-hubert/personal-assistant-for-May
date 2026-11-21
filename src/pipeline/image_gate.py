"""Zero-token image gate: blur rejection and perceptual-hash dedup, CPU only (T2).

The long side is capped at ANALYSIS_LONG_SIDE before scoring so the blur threshold means the
same thing for a 1280px Telegram photo and a 4000px HEIC document.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from dataclasses import dataclass

import cv2
import imagehash
import numpy as np
import pillow_heif
from PIL import Image

from src.domain.media import MAX_BATCH_IMAGES, AssetVerdict

pillow_heif.register_heif_opener()

ANALYSIS_LONG_SIDE = 1024
MAX_HAMMING_FOR_DUPLICATE = 6


@dataclass(frozen=True)
class GateResult:
    verdicts: tuple[AssetVerdict, ...]
    kept_ids: tuple[str, ...]

    @property
    def all_rejected(self) -> bool:
        return not self.kept_ids


@dataclass(frozen=True)
class _Scored:
    asset_id: str
    phash: imagehash.ImageHash
    blur_score: float


def _analyse(asset_id: str, data: bytes) -> _Scored | None:
    try:
        with Image.open(io.BytesIO(data)) as opened:
            gray = opened.convert("L")
    except Exception:
        # Pillow and libheif raise a different type per failure mode; any of them means "unusable".
        return None
    if max(gray.size) > ANALYSIS_LONG_SIDE:
        gray.thumbnail((ANALYSIS_LONG_SIDE, ANALYSIS_LONG_SIDE), Image.Resampling.LANCZOS)
    score = float(cv2.Laplacian(np.asarray(gray), cv2.CV_64F).var())
    return _Scored(asset_id, imagehash.phash(gray), score)


def evaluate(
    images: Sequence[tuple[str, bytes]],
    *,
    blur_threshold: float,
    max_keep: int = MAX_BATCH_IMAGES,
    max_hamming: int = MAX_HAMMING_FOR_DUPLICATE,
) -> GateResult:
    """Keep the sharpest non-duplicate images, at most `max_keep`; kept_ids is sharpest first."""
    scored = {asset_id: _analyse(asset_id, data) for asset_id, data in images}
    sharp = sorted(
        (s for s in scored.values() if s is not None and s.blur_score >= blur_threshold),
        key=lambda s: (-s.blur_score, s.asset_id),
    )
    kept: list[_Scored] = []
    for candidate in sharp:
        if len(kept) == max_keep:
            break
        if all(candidate.phash - other.phash > max_hamming for other in kept):
            kept.append(candidate)
    kept_ids = tuple(s.asset_id for s in kept)

    verdicts = tuple(
        AssetVerdict(
            asset_id=asset_id,
            phash=None if s is None else str(s.phash),
            blur_score=None if s is None else s.blur_score,
            is_blurry=s is not None and s.blur_score < blur_threshold,
            is_kept=asset_id in kept_ids,
        )
        for asset_id, s in scored.items()
    )
    return GateResult(verdicts=verdicts, kept_ids=kept_ids)
