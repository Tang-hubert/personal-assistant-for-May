"""Runtime image fixtures. Sharp = seeded block noise, blurry = the same noise Gaussian-blurred,
duplicate = the same picture re-encoded or dimmed. Nothing binary is committed."""

import io

import cv2
import numpy as np
import pillow_heif
from numpy.typing import NDArray
from PIL import Image

Pixels = NDArray[np.uint8]

pillow_heif.register_heif_opener()

GRID = (100, 75)


def sharp(seed: int, width: int = 800) -> Pixels:
    rng = np.random.default_rng(seed)
    base = rng.integers(0, 256, size=(GRID[1], GRID[0], 3), dtype=np.uint8)
    height = width * GRID[1] // GRID[0]
    return cv2.resize(base, (width, height), interpolation=cv2.INTER_NEAREST)


def blurry(seed: int, width: int = 800) -> Pixels:
    kernel = max(3, int(width * 0.04) // 2 * 2 + 1)
    return cv2.GaussianBlur(sharp(seed, width), (kernel, kernel), 0)


def dimmer(pixels: Pixels, factor: float = 0.97) -> Pixels:
    return cv2.convertScaleAbs(pixels, alpha=factor)


def jpeg(pixels: Pixels, quality: int = 90) -> bytes:
    ok, buf = cv2.imencode(".jpg", pixels, [cv2.IMWRITE_JPEG_QUALITY, quality])
    assert ok
    return buf.tobytes()


def png(pixels: Pixels) -> bytes:
    ok, buf = cv2.imencode(".png", pixels)
    assert ok
    return buf.tobytes()


def heic(pixels: Pixels) -> bytes:
    out = io.BytesIO()
    Image.fromarray(cv2.cvtColor(pixels, cv2.COLOR_BGR2RGB)).save(out, format="HEIF")
    return out.getvalue()
