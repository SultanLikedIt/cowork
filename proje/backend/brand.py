"""Corporate brand: the colours, font and logo every generated file wears.

A brand lives in a folder named by BRAND_DIR (docker-compose mounts
``proje/brand`` at /app/brand):

    brand.json   {"name": "...", "primary": "#001489", "secondary": "#DA291C",
                  "font": "", "logo": "logo.png"}
    logo.png     optional; PNG or JPEG (Word and PowerPoint take no SVG)

With no BRAND_DIR, or no brand.json in it, ``current()`` is inactive and every
renderer keeps its built-in look. The renderers read the brand once, when
they are imported, so a change to brand.json or the logo needs a backend
restart (not a rebuild -- the folder is mounted).

A broken brand.json never fails a document: it is logged and ignored.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

logger = logging.getLogger("agent_webapp.brand")

# The built-in report palette (document_html / document_docx) -- what an
# inactive brand reports, so callers never need a separate default.
DEFAULT_PRIMARY = "2F5D8A"
DEFAULT_SECONDARY = "2F5D8A"

LOGO_SUFFIXES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
MAX_LOGO_BYTES = 2 * 1024 * 1024

_HEX = re.compile(r"^#?([0-9A-Fa-f]{6})$")


def _hex(value: object) -> Optional[str]:
    match = _HEX.match(str(value or "").strip())
    return match.group(1).upper() if match else None


def mix(color: str, other: str, amount: float) -> str:
    """``color`` moved ``amount`` (0..1) of the way towards ``other``."""
    a = [int(color[i : i + 2], 16) for i in (0, 2, 4)]
    b = [int(other[i : i + 2], 16) for i in (0, 2, 4)]
    return "".join(
        f"{round(x + (y - x) * amount):02X}" for x, y in zip(a, b)
    )


@dataclass(frozen=True)
class Brand:
    active: bool = False
    name: str = ""
    primary: str = DEFAULT_PRIMARY
    secondary: str = DEFAULT_SECONDARY
    font: str = ""
    logo_path: Optional[Path] = None

    @property
    def soft(self) -> str:
        """A pale tint of the primary colour, for boxes and callouts."""
        return mix(self.primary, "FFFFFF", 0.9)

    @property
    def strong(self) -> str:
        """Bold body text: the primary colour, darkened."""
        return mix(self.primary, "000000", 0.35)

    def logo(self) -> Optional[tuple[bytes, str]]:
        """(bytes, mime type) of the logo, or None when there is none usable."""
        path = self.logo_path
        if path is None:
            return None
        try:
            data = path.read_bytes()
        except OSError:
            logger.warning("brand logo %s could not be read", path)
            return None
        if not data or len(data) > MAX_LOGO_BYTES:
            logger.warning("brand logo %s is empty or larger than %d bytes", path, MAX_LOGO_BYTES)
            return None
        return data, LOGO_SUFFIXES[path.suffix.lower()]


INACTIVE = Brand()


def load(directory: Optional[str | Path]) -> Brand:
    """The brand in ``directory``, or INACTIVE when there is none."""
    if not directory:
        return INACTIVE
    root = Path(directory)
    config = root / "brand.json"
    if not config.is_file():
        return INACTIVE
    try:
        raw = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("brand.json in %s is unreadable, using the default look: %s", root, exc)
        return INACTIVE
    if not isinstance(raw, dict):
        logger.warning("brand.json in %s is not an object, using the default look", root)
        return INACTIVE

    primary = _hex(raw.get("primary"))
    if primary is None:
        logger.warning("brand.json in %s has no valid primary colour, using the default look", root)
        return INACTIVE
    secondary = _hex(raw.get("secondary")) or primary

    logo_path = None
    logo_name = str(raw.get("logo") or "").strip()
    if logo_name:
        candidate = (root / logo_name).resolve()
        if candidate.parent != root.resolve():
            logger.warning("brand logo %r must be a file inside %s", logo_name, root)
        elif candidate.suffix.lower() not in LOGO_SUFFIXES:
            logger.warning("brand logo %r must be PNG or JPEG", logo_name)
        elif candidate.is_file():
            logo_path = candidate
        else:
            logger.info("brand logo %s not found; documents go without a logo", candidate)

    font = re.sub(r"[^\w .-]", "", str(raw.get("font") or "")).strip()[:60]
    return Brand(
        active=True,
        name=str(raw.get("name") or "").strip()[:80],
        primary=primary,
        secondary=secondary,
        font=font,
        logo_path=logo_path,
    )


@lru_cache(maxsize=1)
def current() -> Brand:
    """The deployment's brand, read once from BRAND_DIR."""
    brand = load(os.environ.get("BRAND_DIR"))
    if brand.active:
        logger.warning(
            "brand active: %s primary=#%s secondary=#%s logo=%s",
            brand.name or "(unnamed)",
            brand.primary,
            brand.secondary,
            brand.logo_path.name if brand.logo_path else "none",
        )
    return brand


__all__ = ["Brand", "INACTIVE", "current", "load", "mix"]
