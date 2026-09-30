"""Fetch an image for report embedding, from SearXNG or Wikimedia Commons.

No API key for either. `fetch_image` searches, downloads a resized
JPEG/PNG, and returns a local path plus attribution. Failure returns None
so the report is still produced without an image.

Two catalogues, tried in that order, because they fail in opposite
directions. Commons is an encyclopedia archive: excellent for a named
dish, building, or person, and the only source here that guarantees a
licence -- but it holds nothing for a modern economic subject, and when
asked for one it answers with whatever it does have (a live
"zeytinyagi uretimi turkiye ekonomisi" returned a Roman press excavation).
The already-running SearXNG's image category covers exactly that gap.

Licence note: SearXNG's web image engines publish no licence metadata, so
results sourced from them are attributed "lisans belirtilmemiş" rather
than claimed as open. Cleanly-licensed engines get a small ranking bonus,
not a monopoly (_CLEAN_LICENSE_ENGINES).

Relevance: candidates are scored against the search query (title + Commons
description metadata). Zero-score hits are skipped so an unrelated file
(e.g. military jet for an olive-tree ask) is not embedded just because it
ranked first. When the user named a subject, that derived query wins over
a model-invented ``image_query``.

That lexical score is pure substring matching, so it cannot tell a real
match from a same-word collision in another language (Commons indexes
Balkan-language captions too) -- a lexically "perfect" candidate still
gets one more check, a fast synchronous LLM call against the document's
topic, before it is downloaded (see ``_llm_verify_relevance``).
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, unquote, urlsplit

import requests

logger = logging.getLogger(__name__)

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
COMMONS_FILE_PAGE = "https://commons.wikimedia.org/wiki/File:"

# Wikimedia requires a descriptive User-Agent; anonymous default UA is blocked.
_USER_AGENT = (
    "agent-webapp-langgraph/1.0 "
    "(document image fetch; https://commons.wikimedia.org/wiki/Commons:API)"
)
_HEADERS = {"User-Agent": _USER_AGENT, "Accept": "application/json"}

_REQUEST_TIMEOUT = 12
_MAX_IMAGE_BYTES = 5 * 1024 * 1024
_THUMB_WIDTH = 800
_MAX_CANDIDATES = 12
_ALLOWED_MIMES = frozenset({"image/jpeg", "image/jpg", "image/png", "image/webp"})

_SEARXNG_BASE_URL = (
    os.environ.get("SEARXNG_BASE_URL") or "http://localhost:8080"
).rstrip("/")

# Pinterest serves hotlink-hostile CDN URLs behind a referer check and
# carries no usable title text, so its results neither download nor rank.
#
# devicons/lucide are icon libraries, not photo archives: a report can
# never be illustrated with a UI glyph, and because their names are single
# words they match on fragments no photo search would -- "bal üreticiliği"
# pulled in "ballerina" and "balloon", which then occupied slots in the
# bounded candidate list ahead of real results.
_IMAGE_ENGINE_DENY = frozenset({"pinterest", "devicons", "lucide", "iconify"})

# Stock agencies that serve a watermarked sales preview at the URL an image
# search indexes. The picture is often exactly right -- an Alamy result for
# "honey beekeeping anatolia" was a correct Anatolian honey bee -- but it
# arrives stamped "alamy" across the frame and "Stock Photo" along the
# bottom, which no report can use. Matched on the host of either URL,
# because the CDN (c8.alamy.com) and the page host differ.
_WATERMARKED_IMAGE_HOSTS = (
    "alamy.com",
    "shutterstock.com",
    "istockphoto.com",
    "gettyimages.",
    "dreamstime.com",
    "depositphotos.com",
    "123rf.com",
    "agefotostock.com",
    "stockphoto",
)

# Below this a picture is a listing thumbnail, not an illustration. Live: a
# correct "woman harvesting olives in Izmir" came back 100x130 and would
# have gone into the PDF as a postage stamp. Checked after decode rather
# than from the search result's own metadata, which is often absent or
# describes the full image while the URL serves a preview.
_MIN_IMAGE_EDGE_PX = 200

# How much any one channel must vary across the frame for the picture to
# be showing something. A solid or near-solid rectangle scores 0.
_MIN_IMAGE_CHANNEL_SPREAD = 24


def _is_watermarked_host(*urls: str) -> bool:
    for url in urls:
        host = urlsplit(url or "").netloc.lower()
        if any(marker in host for marker in _WATERMARKED_IMAGE_HOSTS):
            return True
    return False

# Engines whose results carry a known, embeddable licence. Used only as a
# tie-break bonus, never as a filter: a cleanly-licensed near-match should
# win over an equally-relevant copyrighted one, but relevance stays the
# primary sort so a clearly better photo is not passed over.
_CLEAN_LICENSE_ENGINES = frozenset(
    {"pexels", "unsplash", "openverse", "wikicommons", "wikimedia", "flickr", "artic"}
)


def _has_clean_license(engine: str) -> bool:
    """Matched as a name part, not an exact string.

    SearXNG qualifies several engine names by category -- the Commons
    engine reports itself as "wikicommons.images", not "wikicommons" --
    so an equality test silently never fires for exactly the sources
    this set exists to favour.
    """
    parts = re.split(r"[^a-z0-9]+", (engine or "").lower())
    return any(part in _CLEAN_LICENSE_ENGINES for part in parts if part)

_MIME_BY_EXT = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
}


def _mime_for_url(url: str, declared_format: str = "") -> str:
    """Best-effort MIME for a bare image URL.

    SearXNG rarely fills img_format, and many CDN URLs carry no extension
    at all. Guessing jpeg is safe here because nothing downstream trusts
    it: the bytes are sniffed on arrival (_looks_like_image) and then
    re-encoded by PIL, so a wrong guess is rejected rather than embedded.
    """
    fmt = (declared_format or "").strip().lower().lstrip(".")
    if fmt in _MIME_BY_EXT:
        return _MIME_BY_EXT[fmt]
    path = urlsplit(url).path.lower()
    for ext, mime in _MIME_BY_EXT.items():
        if path.endswith(f".{ext}"):
            return mime
    return "image/jpeg"


# Magic bytes for the formats PIL will accept here. Checked before the
# payload is treated as an image at all: an engine that answers a dead
# hotlink with an HTML error page must not reach the normalize fallback,
# which would otherwise write that HTML out under a .jpg name.
_IMAGE_MAGIC = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a")


def _looks_like_image(payload: bytes) -> bool:
    if payload.startswith(_IMAGE_MAGIC):
        return True
    return payload[:4] == b"RIFF" and payload[8:12] == b"WEBP"

# Generic stopwords (TR + EN) — drop from query tokens / overlap checks.
_STOPWORDS = frozenset(
    {
        "a", "an", "the", "of", "and", "or", "for", "to", "in", "on", "with",
        "from", "into", "about", "image", "photo", "picture", "figure",
        "bir", "ve", "veya", "ya", "da", "de", "ile", "icin", "için", "gibi",
        "olan", "olarak", "bu", "su", "şu", "the", "gorsel", "görsel",
        "fotograf", "fotoğraf", "resim", "telifsiz", "ucretsiz", "ücretsiz",
        # Layout / placement words that leak into Commons queries.
        "altina", "altına", "metnin", "below", "under", "ustune", "üstüne",
    }
)

_TR_FOLD = str.maketrans(
    {
        "ı": "i",
        "İ": "i",
        "ğ": "g",
        "Ğ": "g",
        "ü": "u",
        "Ü": "u",
        "ş": "s",
        "Ş": "s",
        "ö": "o",
        "Ö": "o",
        "ç": "c",
        "Ç": "c",
    }
)


def _fold(text: str) -> str:
    """Case- and accent-fold for loose token matching (TR + Latin)."""
    raw = (text or "").translate(_TR_FOLD)
    decomposed = unicodedata.normalize("NFKD", raw)
    ascii_ish = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return ascii_ish.casefold()


def _sanitize_query(raw: str) -> str:
    text = re.sub(r"[\"'\[\](){}<>]", " ", raw or "")
    text = " ".join(text.split())
    return text[:200].strip()


def _content_tokens(text: str) -> list[str]:
    """Meaningful tokens from a query or filename (stopwords stripped)."""
    folded = _fold(text)
    words = re.findall(r"[a-z0-9]{2,}", folded)
    out: list[str] = []
    seen: set[str] = set()
    for word in words:
        if word in _STOPWORDS or len(word) < 2:
            continue
        if word not in seen:
            seen.add(word)
            out.append(word)
    return out


def _token_overlap_ratio(a: str, b: str) -> float:
    ta = set(_content_tokens(a))
    tb = set(_content_tokens(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta)


# Lone tokens that Commons resolves to the wrong sense (spacecraft, myth, …).
_AMBIGUOUS_IMAGE_TOKENS = frozenset(
    {
        "maven",  # NASA MAVEN Mars orbiter vs Project Maven
        "mercury",
        "apollo",
        "atlas",
        "horizon",
        "oracle",
        "spark",
        # Turkish "pazar" (market/bazaar) is a Balkan loanword Commons also
        # indexes for literal bazaar-town photos ("Pazari i Ri", Tirana;
        # "Novi Pazar"). An "elektrikli araç pazarı" (EV market) report's
        # image query fell through every real-word variant to this lone
        # token and got a Tirana bazaar photo -- verified live: "pazari"
        # scored 1.00 "relevant" against that file's title by substring
        # match alone. "pazar"/"pazari" cover the bare and Turkish-suffix-
        # folded forms (see _content_tokens/_fold).
        "pazar",
        "pazari",
    }
)


# Commons indexes nominative forms. Turkish case suffixes block the match
# outright: "turk kahvesinin" returns 1 result where "turk kahvesi" returns
# 12, so a coffee report fell all the way through to "pisirme" and
# illustrated itself with a picture of cooking. Only the endings that
# actually blocked matches are stripped, and only to ADD a variant -- the
# original is still tried first.
_TR_CASE_SUFFIXES = (
    "nin", "nın", "nun", "nün",
    "ndan", "nden", "dan", "den",
    "nda", "nde", "ta", "te",
    "yi", "yı", "yu", "yü",
)


def _tr_stem(word: str) -> str:
    """Drop one Turkish case ending when a real stem remains."""
    lowered = word.casefold()
    for suffix in _TR_CASE_SUFFIXES:
        if lowered.endswith(suffix) and len(lowered) - len(suffix) >= 4:
            return word[: len(word) - len(suffix)]
    return word


def _query_variants(query: str) -> list[str]:
    """Shorter fallbacks — Commons often returns nothing for long OR-phrases.

    Prefer the full phrase first. For multi-word brand topics
    (\"palantir maven\") Commons may have zero hits for the exact phrase
    while the distinctive brand token alone (\"Palantir\") has photos —
    so add strong single-token fallbacks after the phrase. Skip weak /
    ambiguous lone tokens (anten, maven→NASA MAVEN, …).
    """
    clean = _sanitize_query(query)
    if not clean:
        return []
    variants: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        text = _sanitize_query(value)
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            variants.append(text)

    add(clean)
    # Same phrase with its case endings dropped, tried before any fallback
    # that throws words away -- losing a suffix keeps the subject, losing a
    # word can leave a generic one ("pisirme") standing in for the topic.
    stemmed = " ".join(_tr_stem(w) for w in clean.split())
    if stemmed != clean:
        add(stemmed)
        stem_words = stemmed.split()
        if len(stem_words) >= 3:
            add(" ".join(stem_words[:2]))
    for splitter in (r"\bveya\b", r"\bya da\b", r"\bor\b", r"/", r"\|"):
        parts = re.split(splitter, clean, maxsplit=1, flags=re.IGNORECASE)
        if len(parts) > 1:
            for part in parts:
                part = part.strip()
                # Skip lone weak subjects ("anten") — they match place names.
                if part and not _is_weak_image_query(part):
                    add(part)
                elif part and not _is_weak_image_query(clean):
                    # Keep the strong side only; weak side stays in full query.
                    continue
            break
    words = clean.split()
    if len(words) >= 3 and " " in clean:
        # Only trim when this is not already an OR-alternative list.
        if not re.search(r"(?i)\b(?:veya|ya da|or)\b", clean):
            trimmed = " ".join(words[:2])
            if not _is_weak_image_query(trimmed):
                add(trimmed)
    # Distinctive single-token fallbacks (longest first). Never emit weak
    # or known-ambiguous tokens alone — "maven" → NASA MAVEN spacecraft.
    if len(words) >= 2 and not re.search(r"(?i)\b(?:veya|ya da|or)\b", clean):
        singles = sorted(
            (_content_tokens(w)[0] for w in words if _content_tokens(w)),
            key=len,
            reverse=True,
        )
        for token in singles:
            if (
                len(token) >= 6
                and token not in _AMBIGUOUS_IMAGE_TOKENS
                and not _is_weak_image_query(token)
            ):
                add(token)
    return variants


def _strip_html(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    return " ".join(text.split())


def _ext_value(ext: dict[str, Any], key: str) -> str:
    node = ext.get(key)
    if isinstance(node, dict):
        return _strip_html(str(node.get("value") or ""))
    if isinstance(node, str):
        return _strip_html(node)
    return ""


def _page_text_blob(page: dict[str, Any]) -> str:
    title = str(page.get("title") or "")
    infos = page.get("imageinfo") or []
    ext: dict[str, Any] = {}
    if infos and isinstance(infos[0], dict):
        meta = infos[0].get("extmetadata")
        if isinstance(meta, dict):
            ext = meta
    parts = [
        title,
        _ext_value(ext, "ImageDescription"),
        _ext_value(ext, "ObjectName"),
        _ext_value(ext, "Categories"),
        _ext_value(ext, "Credit"),
    ]
    return " ".join(p for p in parts if p)


def _relevance_score(page: dict[str, Any], query: str) -> float:
    """How well a Commons page matches the search query (0 = reject)."""
    tokens = _content_tokens(query)
    if not tokens:
        return 0.0
    blob = _fold(_page_text_blob(page))
    if not blob:
        return 0.0
    # "Anten" railway station matching query "anten" / "antenna".
    if any(t in {"anten", "antenna", "antennas"} for t in tokens):
        if re.search(
            r"(?i)\b(?:railway|railroad|train\s*station|banstation|"
            r"kilom\.?\s*fr[aå]n|h[oö]jd\s+[oö]ver)\b",
            blob,
        ) and not any(
            t in blob
            for t in (
                "starlink",
                "spacex",
                "satellite",
                "satellit",
                "vsat",
                "dish",
                "parabolic",
                "uydu",
            )
        ):
            return 0.0
    hits = 0.0
    for token in tokens:
        # Prefer whole-token-ish hits; allow stem prefix for short TR/EN words.
        if re.search(rf"(?<![a-z0-9]){re.escape(token)}", blob):
            hits += 1.0
        elif len(token) >= 4 and token in blob:
            hits += 0.6
    if hits <= 0:
        return 0.0
    ratio = hits / len(tokens)
    # Short queries (1–2 tokens) must match every token — otherwise
    # "kutup ayısı" picks a clinic named "Kutup" with zero "ayı".
    need = 1.0 if len(tokens) <= 2 else 0.5
    return ratio if ratio + 1e-9 >= need else 0.0


# Single-token subjects that Commons often resolves to the wrong sense
# (place names, generic hardware). Always pair with a topic/brand token.
_WEAK_IMAGE_SUBJECTS = frozenset(
    {
        "anten",
        "antenna",
        "antennas",
        "dish",
        "satellite",
        "satellites",
        "uydu",
        "terminal",
        "station",
        "internet",
        "network",
        "space",
        "earth",
        "sky",
        "building",
        "photo",
        "image",
    }
)


def _is_weak_image_query(query: str) -> bool:
    tokens = _content_tokens(query)
    if not tokens:
        return True
    return all(t in _WEAK_IMAGE_SUBJECTS for t in tokens)


def _strengthen_image_query(query: str, *contexts: str) -> str:
    """Prefix distinctive topic tokens when the photo subject is too generic."""
    clean = _sanitize_query(query)
    if not clean or not _is_weak_image_query(clean):
        return clean
    ctx_tokens: list[str] = []
    seen: set[str] = set()
    for ctx in contexts:
        for token in _content_tokens(ctx):
            if (
                token in _WEAK_IMAGE_SUBJECTS
                or token in _STOPWORDS
                or token in seen
            ):
                continue
            seen.add(token)
            ctx_tokens.append(token)
            if len(ctx_tokens) >= 2:
                break
        if len(ctx_tokens) >= 2:
            break
    if not ctx_tokens:
        return clean
    return _sanitize_query(" ".join(ctx_tokens + _content_tokens(clean)[:2]))


def _rank_pages(pages: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    scored: list[tuple[float, dict[str, Any]]] = []
    for page in pages:
        score = _relevance_score(page, query)
        if score <= 0:
            title = str(page.get("title") or "")
            logger.info(
                "image_fetch: skip unrelated %s hit %r for query %r",
                page.get("_provider") or "Commons",
                title,
                query,
            )
            continue
        # Small enough that it only separates near-equal candidates: a
        # cleanly-licensed photo wins a tie, but a clearly more relevant
        # one is never demoted below it.
        if _has_clean_license(str(page.get("_engine") or "")):
            score += 0.05
        scored.append((score, page))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [page for _, page in scored]


def _attribution(
    title: str,
    ext: dict[str, Any],
    page_url: str,
    provider: str = "Wikimedia Commons",
) -> tuple[str, str]:
    artist = _ext_value(ext, "Artist") or _ext_value(ext, "Credit") or provider
    license_name = _ext_value(ext, "LicenseShortName") or _ext_value(ext, "License")
    if not license_name:
        # Commons publishes a licence for every file, so a blank one there
        # is still fairly called open. Web image engines publish none at
        # all, and calling that "açık lisans" would assert a permission
        # nobody granted -- say it is unknown so the caller can see what
        # they are embedding.
        license_name = (
            "açık lisans" if provider == "Wikimedia Commons" else "lisans belirtilmemiş"
        )
    display_title = title.removeprefix("File:").replace("_", " ")
    attribution = (
        f"Görsel: {artist}, “{display_title}”. {provider} üzerinden; "
        f"{license_name}. Kaynak: {page_url}"
    )
    return artist, attribution


def _searxng_pages(query: str) -> list[dict[str, Any]]:
    """Image candidates from the SearXNG already running for web search.

    Commons is an encyclopedia archive, not a photo library: nobody
    uploads "2026 Turkish olive oil exports" to it. Asked for a modern,
    economic, or news-shaped subject it returns whatever it does hold --
    verified live, "zeytinyagi uretimi turkiye ekonomisi" produced an
    archaeological dig of a Roman press. The LLM check below cannot save
    that, because there is nothing better in the catalogue to pick; the
    shortage is in the pool, not the ranking.

    SearXNG's image category (google/bing/duckduckgo/pexels/artic) is
    already configured and reachable -- same live query returned 209
    results including an Anadolu Ajansi infographic of exactly this
    subject. This adds no service, credential, or dependency.

    Results are shaped into the Commons page dict the rest of this module
    already consumes, so ranking, LLM verification, and download run
    unchanged.
    """
    resp = requests.get(
        f"{_SEARXNG_BASE_URL}/search",
        params={"q": query, "categories": "images", "format": "json"},
        headers=_HEADERS,
        timeout=_REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    pages: list[dict[str, Any]] = []
    for item in (resp.json().get("results") or [])[: _MAX_CANDIDATES * 3]:
        if not isinstance(item, dict):
            continue
        engine = str(item.get("engine") or "").strip().lower()
        if engine in _IMAGE_ENGINE_DENY:
            continue
        img_src = item.get("img_src")
        if not isinstance(img_src, str) or not img_src.startswith("https://"):
            continue
        if _is_watermarked_host(img_src, str(item.get("url") or "")):
            continue
        thumb = item.get("thumbnail_src") or item.get("thumbnail")
        title = _strip_html(str(item.get("title") or ""))
        pages.append(
            {
                "title": title or query,
                "imageinfo": [
                    {
                        "url": img_src,
                        # Prefer the thumbnail: it is the bounded copy, and
                        # for several engines the full-size URL hotlinks to
                        # an origin that refuses non-browser referers.
                        "thumburl": thumb if isinstance(thumb, str) else None,
                        "mime": _mime_for_url(img_src, str(item.get("img_format") or "")),
                        "extmetadata": {
                            "ImageDescription": {"value": _strip_html(str(item.get("content") or ""))},
                            "Credit": {"value": str(item.get("source") or engine or "")},
                        },
                    }
                ],
                "_page_url": str(item.get("url") or img_src),
                "_provider": str(item.get("source") or engine or "web"),
                "_engine": engine,
            }
        )
        if len(pages) >= _MAX_CANDIDATES:
            break
    return pages


def _search_pages(query: str) -> list[dict[str, Any]]:
    resp = requests.get(
        COMMONS_API,
        params={
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "generator": "search",
            "gsrsearch": f"filetype:bitmap {query}",
            "gsrnamespace": "6",
            "gsrlimit": str(_MAX_CANDIDATES),
            "prop": "imageinfo",
            "iiprop": "url|size|mime|extmetadata",
            "iiurlwidth": str(_THUMB_WIDTH),
        },
        headers=_HEADERS,
        timeout=_REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    pages = (resp.json().get("query") or {}).get("pages") or []
    return [p for p in pages if isinstance(p, dict)]


_PageSearch = Callable[[str], list[dict[str, Any]]]

# Order matters: the web engines go first.
#
# Commons-first was tried, on the theory that the new period/setting rule
# in _llm_verify_relevance would make Commons decline the modern subjects
# it cannot serve, leaving only genuinely unserved queries to reach the
# copyrighted web results. It does not hold. That rule is one LLM call,
# and on the same query it rejected the Roman-press excavation once and
# accepted it the next run -- so Commons-first restores the original bug
# on every run where the check happens to pass.
#
# Preference for a licensed source is therefore expressed where it is
# deterministic instead: SearXNG's own pool includes wikicommons and
# pexels, and _has_clean_license gives those a tie-break bonus. A clean
# source still wins when it is equally relevant; it just no longer wins
# by running first regardless of whether its answer is any good.
_PAGE_SOURCES: tuple[_PageSearch, ...] = (_searxng_pages, _search_pages)


def _pick_download_url(info: dict[str, Any]) -> Optional[str]:
    mime = str(info.get("mime") or "").lower()
    if mime not in _ALLOWED_MIMES:
        return None
    thumb = info.get("thumburl")
    if isinstance(thumb, str) and thumb.startswith("https://"):
        return thumb
    url = info.get("url")
    if isinstance(url, str) and url.startswith("https://"):
        size = info.get("size") or 0
        try:
            if int(size) > _MAX_IMAGE_BYTES:
                return None
        except (TypeError, ValueError):
            pass
        return url
    return None


def _extension_for(content_type: str, url: str) -> str:
    ctype = (content_type or "").split(";")[0].strip().lower()
    if "png" in ctype or url.lower().endswith(".png"):
        return ".png"
    if "webp" in ctype or url.lower().endswith(".webp"):
        return ".webp"
    return ".jpg"


def _is_blank_image(raw: bytes) -> bool:
    """True for a picture with essentially nothing in it.

    A valid JPEG can still be a solid rectangle: a live report embedded a
    668-byte 250x250 image whose average colour was pure black, because
    every check it passed was structural -- it decoded, it was big enough,
    it was not HTML. Nothing looked at whether it depicted anything.

    Measured by extrema rather than a histogram: a photograph, however
    dark, spans a wide range in at least one channel, while a placeholder
    or a failed render is one value everywhere. Fails open on anything
    that will not decode, leaving that to the other checks.
    """
    try:
        from io import BytesIO

        from PIL import Image

        with Image.open(BytesIO(raw)) as image:
            # Extrema on the image itself, not a downsample: shrinking to a
            # thumbnail averages a hundred pixels into one and flattens
            # exactly the variation being measured -- a dark but detailed
            # photograph scored as solid black that way.
            spread = max(hi - lo for lo, hi in image.convert("RGB").getextrema())
    except Exception:  # noqa: BLE001
        return False
    return spread < _MIN_IMAGE_CHANNEL_SPREAD


def _too_small_to_embed(raw: bytes) -> bool:
    """True for a picture too small to illustrate anything.

    Fails open: if PIL is unavailable or the bytes will not decode, this
    says nothing and lets the existing magic-byte and normalize checks
    decide. Its job is to reject a valid but tiny image, not to be a
    second validity check.
    """
    try:
        from io import BytesIO

        from PIL import Image

        with Image.open(BytesIO(raw)) as image:
            width, height = image.size
    except Exception:  # noqa: BLE001
        return False
    return min(width, height) < _MIN_IMAGE_EDGE_PX


def _normalize_image_bytes(raw: bytes, dest_path: Path) -> Path:
    """Re-encode to a baseline JPEG so python-docx/reportlab always accept it.

    Some Commons bitmaps are progressive/CMYK/WebP-labelled JPEGs that
    python-docx rejects with UnrecognizedImageError.
    """
    try:
        from PIL import Image
    except ImportError:
        dest_path.write_bytes(raw)
        return dest_path

    from io import BytesIO

    with Image.open(BytesIO(raw)) as image:
        if image.mode not in {"RGB", "L"}:
            image = image.convert("RGB")
        elif image.mode == "L":
            image = image.convert("RGB")
        # Cap longest side so report pages stay light.
        image.thumbnail((1600, 1600))
        out = dest_path.with_suffix(".jpg")
        image.save(out, format="JPEG", quality=82, optimize=True)
        return out


def _download_first(
    pages: list[dict[str, Any]], dest_dir: Path, slug: str
) -> Optional["ImageResult"]:
    for page in pages:
        infos = page.get("imageinfo") or []
        if not infos or not isinstance(infos[0], dict):
            continue
        info = infos[0]
        image_url = _pick_download_url(info)
        if not image_url:
            continue

        title = str(page.get("title") or "File:image")
        file_name = title.removeprefix("File:")
        provider = str(page.get("_provider") or "") or "Wikimedia Commons"
        page_url = str(page.get("_page_url") or "") or (
            COMMONS_FILE_PAGE + quote(file_name.replace(" ", "_"))
        )
        ext = info.get("extmetadata") if isinstance(info.get("extmetadata"), dict) else {}
        photographer, attribution = _attribution(title, ext, page_url, provider)

        try:
            img_resp = requests.get(
                image_url,
                headers={**_HEADERS, "Accept": "image/*,*/*;q=0.8"},
                timeout=_REQUEST_TIMEOUT,
            )
            img_resp.raise_for_status()
        except (requests.RequestException, OSError) as exc:
            logger.warning("image_fetch: download failed for %s: %s", image_url, exc)
            continue

        if len(img_resp.content) > _MAX_IMAGE_BYTES or not img_resp.content:
            continue
        if _is_blank_image(img_resp.content):
            logger.info("image_fetch: %s is a blank/solid image", image_url)
            continue
        if _too_small_to_embed(img_resp.content):
            logger.info(
                "image_fetch: %s is a listing thumbnail, not an illustration",
                image_url,
            )
            continue
        if not _looks_like_image(img_resp.content):
            # A hotlink an engine indexed but the origin no longer serves
            # answers 200 with an HTML error page. Without this the
            # normalize fallback below writes that HTML out under a .jpg
            # name and the report embeds a file no reader can open.
            logger.info("image_fetch: %s did not return image bytes", image_url)
            continue

        dest_dir.mkdir(parents=True, exist_ok=True)
        safe_slug = re.sub(r"[^a-z0-9]", "-", slug.lower()[:40]).strip("-") or "image"
        raw_path = dest_dir / f"{safe_slug}.bin"
        try:
            dest_path = _normalize_image_bytes(img_resp.content, dest_dir / safe_slug)
        except Exception as exc:  # noqa: BLE001
            logger.warning("image_fetch: normalize failed: %s", exc)
            dest_path = dest_dir / (
                f"{safe_slug}{_extension_for(img_resp.headers.get('content-type', ''), image_url)}"
            )
            dest_path.write_bytes(img_resp.content)
        finally:
            if raw_path.exists():
                raw_path.unlink(missing_ok=True)

        width = info.get("thumbwidth") or info.get("width")
        height = info.get("thumbheight") or info.get("height")
        try:
            width_px = int(width) if width else None
            height_px = int(height) if height else None
        except (TypeError, ValueError):
            width_px = height_px = None

        return ImageResult(
            path=dest_path,
            attribution=attribution,
            photographer=photographer,
            source_url=unquote(page_url),
            width_px=width_px,
            height_px=height_px,
        )
    return None


def _llm_verify_relevance(topic: str, candidates: list[dict[str, Any]]) -> Optional[int]:
    """Ask one fast, synchronous LLM call whether any lexically-ranked
    candidate plausibly depicts/relates to ``topic``.

    The substring-based relevance score above only checks that query
    tokens appear somewhere in a candidate's title/description -- it has
    no notion of what the image actually shows. That let two live,
    verified false positives through as "1.0 relevant": an "elektrikli
    araç pazarı" (EV market) query fell to lone token "pazari" and got a
    Tirana bazaar photo (Turkish/Albanian "pazar" is a shared loanword);
    an "uzay turizmi" (space tourism) query fell to lone token "turizmi"
    and got an unrelated abandoned-hotel photo captioned "Turizmi i
    vjeter" (Albanian for "old tourism"). Denylisting each colliding word
    as found does not generalize -- this asks the model itself whether
    the candidate's own text plausibly matches the topic, using the same
    MODEL_* endpoint as the rest of the app (no new credential).

    Fails safe: any error, timeout, or unparseable reply returns None
    (no image) rather than guessing -- a missing image is always the
    safer wrong answer than an unrelated one.
    """
    if not candidates:
        return None
    import os

    from openai import OpenAI

    from model_config import DEFAULT_API_KEY, DEFAULT_BASE_URL, DEFAULT_MODEL_NAME

    base_url = (os.environ.get("MODEL_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    api_key = os.environ.get("MODEL_API_KEY") or DEFAULT_API_KEY
    model = os.environ.get("MODEL_NAME") or DEFAULT_MODEL_NAME

    capped = candidates[:5]
    lines = [f"{i}: {_page_text_blob(page)[:200]}" for i, page in enumerate(capped)]
    prompt = (
        "You verify an image search result before it is embedded in a "
        f"document. Document topic: {topic!r}\n\n"
        "Candidate images (index: title/description):\n"
        + "\n".join(lines)
        + "\n\nWhich index, if any, plausibly depicts or directly relates "
        "to the topic above? A file whose title only shares a loanword "
        "or homograph with the topic (different country, different "
        "subject) does NOT count. Nor does one that matches the subject "
        "but not its period or setting: for a topic about a present-day "
        "industry, economy, or market, an archaeological, ruined, "
        "ancient, or museum-piece depiction of the same subject is the "
        "wrong image and must be rejected. Reply with ONLY the index "
        "number, or the word none. No other text."
    )
    try:
        client = OpenAI(base_url=base_url, api_key=api_key, timeout=30.0)
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            # DeepSeek reasoning models spend most of a small budget on
            # thinking tokens and return empty content otherwise -- same
            # fix and rationale as artifact_intent.INTENT_MAX_TOKENS /
            # turn_resolution.RESOLVE_MAX_TOKENS: keep headroom for the
            # one-word answer after reasoning. Verified live: max_tokens=16
            # here returned content=None (reasoning ate the whole budget).
            max_tokens=2048,
            temperature=0,
        )
        text = (response.choices[0].message.content or "").strip().lower()
    except Exception as exc:  # noqa: BLE001
        logger.warning("image_fetch: relevance LLM check failed: %s", exc)
        return None
    if "none" in text:
        return None
    match = re.search(r"\d+", text)
    if not match:
        return None
    idx = int(match.group())
    return idx if 0 <= idx < len(capped) else None


def _llm_translate_query(topic: str) -> str:
    """English search phrase for ``topic`` -- Commons captions/categories
    are overwhelmingly English, so a non-English topic's own-language
    variants often exhaust with zero real matches even when Commons has
    good photos: "uzay turizmi" finds nothing usable, but "space tourism"
    (same topic, English) surfaces "EADS Astrium Space Tourism exterior/
    interior.jpg" and four "Space Tourism The New Frontier" photos --
    verified live. Reuses the app's own configured MODEL_* endpoint
    (no external translation API). Fails safe to "" (caller skips this
    variant) on any error or empty reply.
    """
    import os

    from openai import OpenAI

    base_url = (os.environ.get("MODEL_BASE_URL") or "http://localhost:11434/v1").rstrip("/")
    api_key = os.environ.get("MODEL_API_KEY") or "ollama"
    model = os.environ.get("MODEL_NAME") or "qwen2.5:3b"
    try:
        client = OpenAI(base_url=base_url, api_key=api_key, timeout=30.0)
        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "A user asked for a document on this topic, and it "
                        f"should be illustrated with one photo: {topic!r}\n\n"
                        "What should the photo depict? Reply with ONLY a "
                        "short English photo-search phrase (2-4 words) naming "
                        "the real-world SUBJECT to photograph -- never the "
                        "deliverable/format itself (ignore words like report, "
                        "presentation, slide, PDF, Word, document, rapor, "
                        "sunum, belge -- those describe the file, not what "
                        "the photo should show). No quotes, no other text."
                    ),
                }
            ],
            # See _llm_verify_relevance's comment: same model/provider
            # reasoning-token trap, same fix.
            max_tokens=2048,
            temperature=0,
        )
        text = (response.choices[0].message.content or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("image_fetch: query translation failed: %s", exc)
        return ""
    return _sanitize_query(text)


def fetch_image(
    query: str,
    dest_dir: Path,
    *,
    access_key: str = "",
    topic: str = "",
) -> Optional["ImageResult"]:
    """Search Wikimedia Commons and download a small bitmap.

    ``access_key`` is ignored (kept so existing callers do not break).
    Tries shorter query variants when the full phrase returns nothing.
    Candidates must pass BOTH a lexical relevance check against the query
    (cheap, catches obviously unrelated hits) AND an LLM sanity check
    against ``topic`` (falls back to ``query`` when not given) -- the
    lexical check alone lets same-word cross-language collisions through
    (see ``_llm_verify_relevance``).

    When every own-language variant exhausts with nothing verified, tries
    one more: an LLM-translated English phrase for ``topic``. Commons'
    captions/categories are overwhelmingly English, so a perfectly good
    non-English topic ("uzay turizmi") can otherwise never find the real,
    on-topic photos that exist under their English name ("space tourism"
    -> "EADS Astrium Space Tourism exterior.jpg", verified live) --
    without this, the LLM check above only prevents a WRONG photo, it
    does not help find the RIGHT one for such topics.
    """
    del access_key
    variants = _query_variants(query)
    if not variants:
        return None
    topic = topic or query

    def try_variant(
        variant: str, search: _PageSearch
    ) -> tuple[Optional["ImageResult"], Optional[BaseException]]:
        try:
            pages = search(variant)
        except (requests.RequestException, OSError, ValueError) as exc:
            logger.warning(
                "image_fetch: %s search failed for %r: %s", search.__name__, variant, exc
            )
            return None, exc
        ranked = _rank_pages(pages, variant)
        if not ranked:
            return None, None
        verified_idx = _llm_verify_relevance(topic, ranked)
        if verified_idx is None:
            logger.info(
                "image_fetch: LLM rejected all lexical candidates for %r (topic %r)",
                variant,
                topic,
            )
            return None, None
        return _download_first([ranked[verified_idx]], dest_dir, slug=variant), None

    last_error: Optional[BaseException] = None

    # English first, not as a last resort.
    #
    # It was a fallback at first, which cannot work: the failure it has to
    # catch is not "found nothing", it is "found something bad and stopped
    # looking". Live, "bal ureticiligi" succeeded immediately on a
    # Milliyet story headlined "Süt ve bal üreticiliği için destek" whose
    # photo illustrates the milk half of it, so a honey report got a cow --
    # and both the lexical score and the LLM check passed it, correctly,
    # because the headline really is about honey production.
    #
    # The reason English does better is not that it returns more, but
    # WHERE it returns from. Turkish image content sits mostly on news
    # sites, whose title describes the article; Commons, Pexels and
    # Unsplash caption the picture itself, and index in English. Nothing
    # in this module ever looks at the image, so searching where the text
    # describes it is the whole defence.
    #
    # Measured against the native query on four topics: identical quality
    # on the Turkey-specific ones (türk kahvesi, türk hamamı -- so this
    # costs nothing where Turkish was already right) and a correct result
    # instead of a wrong one on the general ones (bal üreticiliği,
    # zeytinyağı üretimi). Native variants still run after it, so a
    # subject that only exists under its Turkish name is not lost.
    translated = _llm_translate_query(topic)
    attempts = list(variants)
    if translated and _fold(translated) not in {_fold(v) for v in variants}:
        attempts.insert(0, translated)

    # Each source is exhausted over every query variant before the next is
    # tried, rather than interleaving. The two catalogues fail in opposite
    # directions -- SearXNG is thin on encyclopedic subjects, Commons on
    # modern/economic ones -- so a variant that finds nothing in one is
    # weak evidence about the other, and finishing a source first keeps
    # the ordering of results within it meaningful.
    for search in _PAGE_SOURCES:
        for variant in attempts:
            result, error = try_variant(variant, search)
            if error is not None:
                last_error = error
            if result is not None:
                return result

    if last_error is not None:
        logger.warning("image_fetch: all variants failed; last error: %s", last_error)
    else:
        logger.info("image_fetch: no usable image for query %r", query)
    return None


class ImageResult:
    """Result of a successful image fetch."""

    __slots__ = ("path", "attribution", "photographer", "source_url", "width_px", "height_px")

    def __init__(
        self,
        path: Path,
        attribution: str,
        photographer: str,
        source_url: str,
        width_px: Optional[int] = None,
        height_px: Optional[int] = None,
    ) -> None:
        self.path = path
        self.attribution = attribution
        self.photographer = photographer
        self.source_url = source_url
        self.width_px = width_px
        self.height_px = height_px


_IMAGE_ASK_RE = re.compile(
    r"(?is)\b(?:görsel\w*|gorsel\w*|foto(?:ğraf|graf)?\w*|resim\w*|"
    r"image\w*|photo\w*|picture\w*|visuals?)\b"
)
# Noun phrase immediately before "görseli" — prefer the subject after
# "telifsiz/bir", and never start on layout words like "altına".
_IMAGE_OBJECT_TR = re.compile(
    r"(?is)(?:metnin\s+)?(?:alt[ıi]na\s+|altina\s+)?"
    r"(?:internetten\s+)?(?:buldu[gğ]un(?:uz)?\s+)?"
    r"(?:telifsiz\s+)?(?:bir\s+)?"
    r"([A-Za-zÇĞİÖŞÜçğıöşü](?:[\wÇĞİÖŞÜçğıöşü\-]+(?:\s+[\wÇĞİÖŞÜçğıöşü\-]+){0,5}))"
    r"\s+(?:görsel(?:i|ini)|gorsel(?:i|ini)|"
    r"foto(?:ğraf|graf)(?:ı|i|ını|ini)?|resm(?:i|ini))\b"
)
_IMAGE_OBJECT_EN = re.compile(
    r"(?is)(?:royalty[-\s]?free\s+)?(?:an?\s+)?"
    r"(?:image|photo|picture)\s+of\s+"
    r"([A-Za-z][^.!?\n]{2,60})"
)
# Topic cues: "Palantir Maven ile ilgili …", "… üzerindeki etkisini …"
_TOPIC_TR = re.compile(
    r"(?is)(?:bana\s+)?(.{3,80}?)\s+"
    r"(?:ile\s+ilgili|hakkında|hakkinda|konulu|"
    r"üzerine|uzerine|üzerindeki|uzerindeki)\b"
)
_TOPIC_EN = re.compile(
    r"(?is)\b(?:about|on|regarding)\s+(.{3,70}?)(?:"
    r"\s+(?:and|,|\.|then|for|with|write|create|make|research)\b|$)"
)
_QUERY_NOISE = re.compile(
    r"(?is)\b(?:"
    r"internetten|buldu[gğ]un(?:uz)?|bul|ekle|koy|ekleyin|eklemeni|"
    r"telifsiz|ücretsiz|lisanss[ıi]z|bir|veya|ya da|ve|ile|"
    r"metnin\s+alt[ıi]na|alt[ıi]na|altina|döküman|dokuman|belge|sayfa|"
    r"from\s+the\s+internet|royalty[-\s]?free|please|add|include|"
    r"below\s+the\s+text|under\s+the\s+text|below|under|"
    r"bana|yap(?:ıp|ip)?|yazmanı|hazırla(?:manı)?|istiyorum|mümkünse|mumkunse|"
    r"sunumu?|zenginleştir(?:erek)?|zenginlestir|"
    # Suffixed forms too: "\bpdf\b" never matched "pdfi"/"pdfe", so the
    # format word survived into the subject.
    r"word\w*|docx?\w*|html\w*|pdf\w*|pptx?\w*|powerpoint\w*|"
    # Where the file should end up, and the verb for it. "fotograf da koy
    # pdfe" has no subject before the noun, so the pattern above swept up
    # the tail of "bilgisayarimda da ac bu pdfi" instead and Commons was
    # searched for "da ac bu".
    r"bilgisayar\w*|makine\w*|masa[üu]st[üu]\w*|lokal\w*|yerel\w*|"
    r"a[çc]|ba[şs]lat|indir|kaydet|g[öo]nder|"
    r"ayn[ıi]\s+zamanda|bu|[şs]u|"
    r"araştırma|arastirma|sistemi?|detaylar(?:ını|ini)?|"
    r"etkisini|etkileri|kısaca|kisaca|özet|ozet|"
    r"\bya\b"
    r")\b"
)
# Queries that are clearly about the deliverable, not the photo subject.
_FLUFF_QUERY = re.compile(
    r"(?is)^(?:html|word|pdf|docx?|pptx?|sunum|dokuman|döküman|rapor|"
    r"ozet|özet|belge|sayfa|anından|hazirlama|hazırlama|ternet|"
    r"internetten)\b|"
    r"\b(?:html\s+sunum|word\s+d[oö]k[uü]man)"
)


def request_wants_image(text: str) -> bool:
    """True when the user asked to put a photo/image in the deliverable."""
    return bool(_IMAGE_ASK_RE.search(text or ""))


def _looks_like_fluff_query(query: str) -> bool:
    tokens = _content_tokens(query)
    if not tokens:
        return True
    if _FLUFF_QUERY.search(_sanitize_query(query)):
        return True
    # Only format/deliverable words left after tokenization.
    deliverable = {
        "html", "word", "pdf", "docx", "pptx", "ppt", "sunum", "dokuman",
        "rapor", "ozet", "belge", "sayfa", "anindan", "gorsel", "image",
        "photo", "picture", "visual", "hazirlama", "hazirla",
    }
    return all(t in deliverable for t in tokens)


def _topic_query_from_request(text: str) -> str:
    """Best-effort document topic when user said 'add images' without naming one."""
    body = text or ""
    chunk = ""
    match = _TOPIC_TR.search(body)
    if match:
        chunk = match.group(1)
    else:
        match = _TOPIC_EN.search(body)
        if match:
            chunk = match.group(1)
    cleaned = _QUERY_NOISE.sub(" ", chunk)
    cleaned = _sanitize_query(cleaned)
    words = cleaned.split()
    # Prefer distinctive multi-word topics (brand + product).
    if len(words) > 4:
        cleaned = " ".join(words[:4])
    if _looks_like_fluff_query(cleaned):
        return ""
    return cleaned[:120]


def image_query_from_request(text: str, *, fallback: str = "") -> str:
    """Derive a Commons search query from the user ask (no LLM).

    Order:
    1. Explicit subject (\"zeytin ağacı görseli\", \"photo of X\").
    2. Document topic (\"X ile ilgili …\") when user only asked to enrich
       with images without naming the photo subject.
    3. Fallback title / empty.
    """
    body = text or ""
    chunk = ""
    match = _IMAGE_OBJECT_TR.search(body)
    if match:
        chunk = match.group(1)
    else:
        match = _IMAGE_OBJECT_EN.search(body)
        if match:
            chunk = match.group(1)
    if chunk:
        # Clean each "A veya/or B" alternative separately so "veya" is not
        # stripped as noise and we do not glue subjects into "A B".
        alts = re.split(r"(?i)\b(?:veya|ya da|or)\b", chunk)
        cleaned_alts: list[str] = []
        for alt in alts:
            piece = _QUERY_NOISE.sub(" ", alt)
            piece = _sanitize_query(piece)
            words = piece.split()
            if len(words) > 3:
                piece = " ".join(words[:3])
            if piece and not _looks_like_fluff_query(piece):
                cleaned_alts.append(piece)
        if cleaned_alts:
            return " veya ".join(cleaned_alts)[:120]

    topic = _topic_query_from_request(body)
    if topic:
        return topic

    cleaned = _sanitize_query(fallback)
    if cleaned and not _looks_like_fluff_query(cleaned):
        words = cleaned.split()
        if len(words) > 4:
            cleaned = " ".join(words[:4])
        return cleaned[:120]
    return ""


def resolve_image_query(
    original_request: str,
    *,
    model_query: str = "",
    fallback: str = "",
) -> str:
    """Pick the search query: user subject / topic beats a model-invented phrase.

    Document models sometimes invent unrelated ``image_query`` values
    (wrong topic, place name, news headline). When the user already named
    what to illustrate — or named the research topic — that phrase wins.
    Weak lone subjects (\"anten\") are strengthened with the report topic
    so Commons does not return a railway station named Anten.
    """
    model = _sanitize_query(model_query)
    # The topic is derived whether or not the user asked for a picture.
    # Gating it behind request_wants_image left the model's invented query
    # completely unchecked on every report nobody asked to illustrate --
    # which is how a polar-bear study ended up searching Commons for
    # "bilgisayarimda da ac bu", the tail of the delivery instruction, and
    # saving it as da-ac-bu.jpg. The overlap test below is exactly the
    # guard this docstring promises; it just never ran in that case.
    derived = image_query_from_request(original_request or "", fallback=fallback)
    if not derived:
        derived = _topic_query_from_request(original_request or "")
    picked = ""
    if derived and not _looks_like_fluff_query(derived):
        if model and not _looks_like_fluff_query(model):
            if _token_overlap_ratio(derived, model) >= 0.5:
                picked = model if len(model.split()) <= 5 else derived
            else:
                picked = derived
        else:
            picked = derived
    elif model and not _looks_like_fluff_query(model):
        picked = model
    else:
        topic = _topic_query_from_request(original_request or "")
        if topic:
            picked = topic
        else:
            fb = _sanitize_query(fallback)
            if fb and not _looks_like_fluff_query(fb):
                picked = fb
            else:
                picked = derived or model or fb
    return _strengthen_image_query(
        picked, original_request or "", fallback or "", model or ""
    )
