from __future__ import annotations

import hashlib
import io
import re
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from PIL import Image, ImageOps, UnidentifiedImageError

try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:  # pragma: no cover - dependency is present in deployed installs.
    pass


MEBIBYTE = 1024 * 1024
MAX_SINGLE_UPLOAD_BYTES = 25 * MEBIBYTE
MAX_RAW_BATCH_BYTES = 256 * MEBIBYTE
MAX_SESSION_IMAGE_BYTES = 60 * MEBIBYTE
MAX_PREPARED_IMAGE_BYTES = 8 * MEBIBYTE
MIN_TARGET_IMAGE_BYTES = 96 * 1024
MAX_IMAGE_PIXELS = 60_000_000
MIN_LONG_EDGE = 768
SUPPORTED_IMAGE_EXTENSIONS = ("jpg", "jpeg", "png", "webp", "heic", "heif")

Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS

_SAFE_NAME = re.compile(r"[^\w.()\- ]+", flags=re.UNICODE)
_QUALITY_STEPS = (90, 85, 80, 75, 70, 65, 60, 55)


class ImageInputError(ValueError):
    """A safe, user-facing image validation or processing error."""


@dataclass(frozen=True)
class PreparedImage:
    name: str
    mime_type: str
    data: bytes
    width: int
    height: int
    source_sha256: str
    sha256: str

    @property
    def byte_size(self) -> int:
        return len(self.data)

    def as_session_record(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "mime_type": self.mime_type,
            "data": self.data,
            "width": self.width,
            "height": self.height,
            "source_sha256": self.source_sha256,
            "sha256": self.sha256,
            "byte_size": self.byte_size,
        }


@dataclass(frozen=True)
class PreparedBatch:
    images: tuple[PreparedImage, ...]
    duplicate_names: tuple[str, ...]

    @property
    def total_bytes(self) -> int:
        return sum(image.byte_size for image in self.images)


def _safe_name(value: Any) -> str:
    name = Path(str(value or "image")).name
    name = _SAFE_NAME.sub("_", name).strip(" ._")
    return (name or "image")[:120]


def _file_bytes(uploaded_file: Any) -> bytes:
    getter = getattr(uploaded_file, "getvalue", None)
    if callable(getter):
        data = getter()
    else:
        reader = getattr(uploaded_file, "read", None)
        data = reader() if callable(reader) else b""
    if not isinstance(data, bytes):
        data = bytes(data)
    return data


def _rgb_image(source: Image.Image) -> Image.Image:
    image = ImageOps.exif_transpose(source).copy()
    if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(background, rgba).convert("RGB")
    return image.convert("RGB")


def _bounded_size(width: int, height: int) -> tuple[int, int]:
    if width >= height:
        return (4096, 2160)
    return (2160, 4096)


def _encode_jpeg(image: Image.Image, *, quality: int, icc_profile: bytes | None) -> bytes:
    output = io.BytesIO()
    save_options: dict[str, Any] = {
        "format": "JPEG",
        "quality": quality,
        "optimize": True,
        "progressive": True,
    }
    if icc_profile and len(icc_profile) <= 512 * 1024:
        save_options["icc_profile"] = icc_profile
    image.save(output, **save_options)
    return output.getvalue()


def _fit_to_target(
    image: Image.Image,
    *,
    target_bytes: int,
    icc_profile: bytes | None,
) -> tuple[bytes, int, int]:
    working = image.copy()
    working.thumbnail(_bounded_size(*working.size), Image.Resampling.LANCZOS)

    latest = b""
    for quality in _QUALITY_STEPS:
        latest = _encode_jpeg(working, quality=quality, icc_profile=icc_profile)
        if len(latest) <= target_bytes:
            return latest, working.width, working.height

    while max(working.size) > MIN_LONG_EDGE:
        ratio = (target_bytes / max(len(latest), 1)) ** 0.5
        scale = min(0.88, max(0.65, ratio * 0.94))
        current_long_edge = max(working.size)
        next_long_edge = max(MIN_LONG_EDGE, int(current_long_edge * scale))
        if next_long_edge >= current_long_edge:
            break
        scale = next_long_edge / current_long_edge
        next_size = (
            max(1, int(working.width * scale)),
            max(1, int(working.height * scale)),
        )
        working = working.resize(next_size, Image.Resampling.LANCZOS)
        for quality in (75, 65, 55, 48):
            latest = _encode_jpeg(working, quality=quality, icc_profile=icc_profile)
            if len(latest) <= target_bytes:
                return latest, working.width, working.height

    raise ImageInputError(
        "לא ניתן לדחוס אחת מהתמונות מספיק בלי לפגוע משמעותית באיכות. "
        "נסה לשלוח פחות תמונות בהודעה אחת."
    )


def _prepare_one(*, name: str, raw: bytes, target_bytes: int, source_sha256: str) -> PreparedImage:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as source:
                if getattr(source, "n_frames", 1) > 1:
                    raise ImageInputError("תמונות מונפשות אינן נתמכות. שלח תמונה סטטית.")
                source.load()
                icc_profile = source.info.get("icc_profile")
                if not isinstance(icc_profile, bytes):
                    icc_profile = None
                image = _rgb_image(source)
    except ImageInputError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ImageInputError(f"התמונה {name} גדולה מדי לעיבוד בטוח.") from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageInputError(f"הקובץ {name} אינו תמונה תקינה או נתמכת.") from exc

    data, width, height = _fit_to_target(
        image,
        target_bytes=target_bytes,
        icc_profile=icc_profile,
    )
    return PreparedImage(
        name=name,
        mime_type="image/jpeg",
        data=data,
        width=width,
        height=height,
        source_sha256=source_sha256,
        sha256=hashlib.sha256(data).hexdigest(),
    )


def prepare_uploads(
    uploaded_files: Sequence[Any],
    *,
    existing_image_bytes: int = 0,
    existing_source_hashes: Iterable[str] = (),
) -> PreparedBatch:
    """Validate, normalize, de-identify, and size a batch for the Kimi request budget."""
    if existing_image_bytes < 0:
        raise ValueError("existing_image_bytes cannot be negative")

    remaining_bytes = MAX_SESSION_IMAGE_BYTES - existing_image_bytes
    if remaining_bytes < MIN_TARGET_IMAGE_BYTES:
        raise ImageInputError("מכסת התמונות בשיחה מלאה. נקה את השיחה כדי לצרף תמונות נוספות.")

    seen_sources = set(existing_source_hashes)
    raw_items: list[tuple[str, bytes, str]] = []
    duplicate_names: list[str] = []
    raw_batch_bytes = 0

    for uploaded_file in uploaded_files:
        name = _safe_name(getattr(uploaded_file, "name", "image"))
        raw = _file_bytes(uploaded_file)
        if not raw:
            raise ImageInputError(f"הקובץ {name} ריק.")
        if len(raw) > MAX_SINGLE_UPLOAD_BYTES:
            raise ImageInputError(f"התמונה {name} גדולה מ־25MB.")

        raw_batch_bytes += len(raw)
        if raw_batch_bytes > MAX_RAW_BATCH_BYTES:
            raise ImageInputError(
                "הקבצים המקוריים בהודעה חורגים מ־256MB. "
                "שלח אותם בשתי הודעות או הקטן אותם קודם."
            )

        source_sha256 = hashlib.sha256(raw).hexdigest()
        if source_sha256 in seen_sources:
            duplicate_names.append(name)
            continue
        seen_sources.add(source_sha256)
        raw_items.append((name, raw, source_sha256))

    if not raw_items:
        return PreparedBatch(images=(), duplicate_names=tuple(duplicate_names))

    target_bytes = min(MAX_PREPARED_IMAGE_BYTES, remaining_bytes // len(raw_items))
    if target_bytes < MIN_TARGET_IMAGE_BYTES:
        raise ImageInputError(
            "מספר התמונות גדול מדי לנפח שנותר בשיחה. "
            "נקה את השיחה או שלח פחות תמונות."
        )

    prepared = tuple(
        _prepare_one(
            name=name,
            raw=raw,
            target_bytes=target_bytes,
            source_sha256=source_sha256,
        )
        for name, raw, source_sha256 in raw_items
    )
    if sum(image.byte_size for image in prepared) > remaining_bytes:
        raise ImageInputError("התמונות חורגות ממכסת השיחה לאחר עיבוד.")
    return PreparedBatch(images=prepared, duplicate_names=tuple(duplicate_names))
