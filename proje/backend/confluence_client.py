"""Read-only Confluence REST client (Cloud and Server / Data Center).

One endpoint does all the work: ``GET /rest/api/content/search`` with a CQL
query. It exists on Cloud (under ``/wiki``) and on Server/DC, it can expand
the page body, and -- the reason it is used even to read a single page -- a
page read is then ``id = 123 AND space in (...)``: the space allowlist is
part of the query Confluence itself evaluates, not only a check here.

Credentials are the Jira ones by default (``JIRA_USERNAME`` +
``JIRA_API_TOKEN``, or ``JIRA_PERSONAL_TOKEN``): on Atlassian Cloud one
account and one API token cover both products, and Cloud's Confluence lives
at ``<jira site>/wiki``. ``CONFLUENCE_*`` settings override each piece for a
Confluence that runs elsewhere.

Security notes
--------------
* **Read-only by construction.** Every request is a GET; there is no method
  that creates, edits, comments or moves anything.
* **Space allowlist.** ``CONFLUENCE_SPACES`` is required: without it the
  integration counts as not configured. Every query this client sends is
  restricted to those spaces (``build_space_clause``), and ``get_page``
  re-checks the space of what came back.
* **The host is fixed by configuration**, redirects are never followed (a
  redirect from the REST API is an SSO page), and the token never appears
  in a log or an error message (``ConfluenceConfig`` hides it from repr).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

stabilization_logger = logging.getLogger("agent_webapp.stabilization")

DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
MAX_RESPONSE_BYTES = 6 * 1024 * 1024
MAX_SEARCH_RESULTS = 50
MAX_ERROR_CHARS = 400
MAX_SPACES = 30

MAX_CONCURRENT_CALLS = int(os.environ.get("CONFLUENCE_MAX_CONCURRENT") or 4)
MAX_QUEUE_WAIT_SECONDS = 60
_CALL_SLOTS = threading.BoundedSemaphore(max(1, MAX_CONCURRENT_CALLS))

_USER_AGENT = "agent-webapp/1.0 (+confluence read-only)"
SEARCH_PATH = "/rest/api/content/search"

# Confluence's own rule for space keys (personal spaces start with "~").
SPACE_KEY_RE = re.compile(r"^~?[A-Za-z0-9_]{1,255}$")
PAGE_ID_RE = re.compile(r"^[1-9][0-9]{0,19}$")

FATAL_CODES = frozenset(
    {"not_configured", "dns", "unreachable", "tls", "auth", "redirect", "busy"}
)


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def _flag(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().casefold() in {"1", "true", "yes", "on"}


def _float(env: Mapping[str, str], name: str, default: float, *, low: float, high: float) -> float:
    try:
        value = float(str(env.get(name) or "").strip() or default)
    except ValueError:
        return default
    return max(low, min(value, high))


def parse_spaces(raw: Any) -> tuple[str, ...]:
    """``"ENG, hr ,~ali"`` -> ("ENG", "HR", "~ali"); invalid keys dropped."""
    keys: list[str] = []
    for part in re.split(r"[,\s;]+", str(raw or "")):
        key = part.strip()
        if not key or not SPACE_KEY_RE.match(key):
            continue
        # Personal space keys are case-sensitive usernames; the rest are
        # upper-case by Confluence's convention.
        key = key if key.startswith("~") else key.upper()
        if key not in keys:
            keys.append(key)
        if len(keys) >= MAX_SPACES:
            break
    return tuple(keys)


@dataclass(frozen=True)
class ConfluenceConfig:
    enabled: bool = False
    base_url: str = ""
    # "cloud" | "server" (Server covers Data Center: same API).
    deployment: str = "server"
    spaces: tuple[str, ...] = ()
    username: str = ""
    api_token: str = field(default="", repr=False)
    personal_token: str = field(default="", repr=False)
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS

    @property
    def is_cloud(self) -> bool:
        return self.deployment == "cloud"

    @property
    def auth_mode(self) -> str:
        if self.personal_token and not self.is_cloud:
            return "pat"
        if self.username and self.api_token:
            return "basic"
        return ""

    def problems(self) -> list[str]:
        found: list[str] = []
        if not self.enabled:
            found.append("CONFLUENCE_ENABLED kapalı.")
        if not self.base_url:
            found.append("Confluence adresi yok: CONFLUENCE_URL (ya da Cloud için JIRA_URL) boş.")
        else:
            parts = urlsplit(self.base_url)
            if parts.scheme not in {"http", "https"} or not parts.hostname:
                found.append("CONFLUENCE_URL http:// veya https:// ile başlayan tam bir adres olmalı.")
            elif parts.query or parts.fragment:
                found.append("CONFLUENCE_URL sorgu veya # içermemeli.")
        if not self.spaces:
            found.append(
                "CONFLUENCE_SPACES boş: ajanın okuyabileceği space anahtarlarını "
                "virgülle yazın (ör. ENG,HR)."
            )
        if not self.auth_mode:
            found.append(
                "Confluence kimlik bilgisi yok: JIRA_USERNAME + JIRA_API_TOKEN "
                "(Cloud) ya da JIRA_PERSONAL_TOKEN (Server/DC), veya CONFLUENCE_* karşılıkları."
            )
        return found

    @property
    def configured(self) -> bool:
        return not self.problems()

    def page_url(self, webui: str) -> str:
        """Absolute link for a ``_links.webui`` path Confluence returned."""
        path = str(webui or "")
        if not path.startswith("/") or path.startswith("//"):
            return ""
        return self.base_url + path


def _detect_deployment(base_url: str) -> str:
    host = (urlsplit(base_url).hostname or "").lower()
    if host.endswith(".atlassian.net") or host.endswith(".jira.com"):
        return "cloud"
    return "server"


def load_config(environ: Optional[Mapping[str, str]] = None) -> ConfluenceConfig:
    """Read ``CONFLUENCE_*``, falling back to the ``JIRA_*`` account.

    Read on every call, never cached, so tests can pass their own mapping.
    """
    env = os.environ if environ is None else environ
    jira_url = str(env.get("JIRA_URL") or "").strip().rstrip("/")
    base_url = str(env.get("CONFLUENCE_URL") or "").strip().rstrip("/")
    if not base_url and jira_url and _detect_deployment(jira_url) == "cloud":
        # Atlassian Cloud: Confluence is the same site under /wiki.
        base_url = f"{urlsplit(jira_url).scheme}://{urlsplit(jira_url).netloc}/wiki"
    deployment = str(env.get("CONFLUENCE_DEPLOYMENT") or "auto").strip().lower()
    if deployment not in {"cloud", "server"}:
        deployment = _detect_deployment(base_url)

    def pick(name: str) -> str:
        own = str(env.get(f"CONFLUENCE_{name}") or "").strip()
        return own or str(env.get(f"JIRA_{name}") or "").strip()

    return ConfluenceConfig(
        enabled=_flag(env, "CONFLUENCE_ENABLED", False),
        base_url=base_url,
        deployment=deployment,
        spaces=parse_spaces(env.get("CONFLUENCE_SPACES")),
        username=pick("USERNAME"),
        api_token=pick("API_TOKEN"),
        personal_token=pick("PERSONAL_TOKEN"),
        timeout_seconds=_float(
            env, "CONFLUENCE_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS, low=3, high=120
        ),
        connect_timeout_seconds=_float(
            env,
            "CONFLUENCE_CONNECT_TIMEOUT_SECONDS",
            DEFAULT_CONNECT_TIMEOUT_SECONDS,
            low=1,
            high=60,
        ),
    )


def is_configured() -> bool:
    """Enabled and complete -- not a reachability check (see main routing)."""
    return load_config().configured


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class ConfluenceError(Exception):
    """A failed Confluence call, classified by what would fix it."""

    def __init__(
        self, code: str, detail: str, *, remedy: str = "", status: Optional[int] = None
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.remedy = remedy
        self.status = status

    @property
    def fatal(self) -> bool:
        return self.code in FATAL_CODES


_REMEDY_NETWORK = (
    "Confluence şirket ağından/VPN'den erişilebiliyorsa VPN bağlantısını kontrol "
    "edin; CONFLUENCE_URL'nin doğru olduğundan emin olun (docs/confluence.md)."
)
_REMEDY_AUTH = (
    "Cloud: JIRA_USERNAME hesabın e-posta adresi, JIRA_API_TOKEN id.atlassian.com'da "
    "üretilmiş token olmalı. Server/DC: Confluence profilinden üretilmiş bir "
    "Personal Access Token (CONFLUENCE_PERSONAL_TOKEN)."
)
_REMEDY_TLS = (
    "Sunucu sertifikası doğrulanamadı; şirket içi bir sertifika otoritesi "
    "kullanılıyor olabilir (docs/jira.md → Sertifika)."
)
_NAME_RESOLUTION_HINTS = (
    "NameResolutionError",
    "Name or service not known",
    "nodename nor servname",
    "getaddrinfo failed",
    "Temporary failure in name resolution",
    "No address associated with hostname",
)


def _confluence_message(raw: bytes) -> str:
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, AttributeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    message = payload.get("message")
    if isinstance(message, str):
        return message[:MAX_ERROR_CHARS]
    return ""


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------


def cql_string(value: str) -> str:
    """A CQL double-quoted literal: backslash and quote escaped, no newlines."""
    text = " ".join(str(value or "").split())
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_space_clause(spaces: tuple[str, ...]) -> str:
    return "space in (" + ", ".join(cql_string(key) for key in spaces) + ")"


@dataclass(frozen=True)
class SearchPage:
    results: list[dict[str, Any]]
    total: Optional[int] = None
    truncated: bool = False


class ConfluenceClient:
    """One configured Confluence. ``session`` is injectable for tests."""

    def __init__(self, config: ConfluenceConfig, *, session: Any = None) -> None:
        self.config = config
        if session is None:
            import requests

            session = requests.Session()
        self._session = session

    # --- transport ---------------------------------------------------------

    def _url(self, path: str) -> str:
        if not path.startswith("/rest/"):
            raise ConfluenceError("bad_request", "geçersiz Confluence yolu")
        url = self.config.base_url + path
        if not url.startswith(self.config.base_url + "/rest/"):
            raise ConfluenceError("bad_request", "Confluence isteği yapılandırılan adresin dışına çıkıyor")
        return url

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": _USER_AGENT}
        if self.config.auth_mode == "pat":
            headers["Authorization"] = f"Bearer {self.config.personal_token}"
        return headers

    def _auth(self) -> Optional[tuple[str, str]]:
        if self.config.auth_mode == "basic":
            return (self.config.username, self.config.api_token)
        return None

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        if not self.config.configured:
            raise ConfluenceError(
                "not_configured",
                "Confluence yapılandırılmamış: " + " ".join(self.config.problems()),
                remedy="docs/confluence.md içindeki ayarları .env dosyasına girin.",
            )
        import requests

        url = self._url(path)
        if not _CALL_SLOTS.acquire(timeout=MAX_QUEUE_WAIT_SECONDS):
            raise ConfluenceError(
                "busy",
                f"Eşzamanlı Confluence isteği sınırı ({MAX_CONCURRENT_CALLS}) dolu ve sıra açılmadı.",
            )
        started = time.monotonic()
        try:
            with self._session.request(
                "GET",
                url,
                params=params,
                headers=self._headers(),
                auth=self._auth(),
                timeout=(self.config.connect_timeout_seconds, self.config.timeout_seconds),
                allow_redirects=False,
                stream=True,
            ) as response:
                status = int(response.status_code)
                if 300 <= status < 400:
                    raise ConfluenceError(
                        "redirect",
                        f"Confluence isteği başka bir adrese yönlendirdi (HTTP {status}); "
                        "büyük ihtimalle bir SSO giriş sayfası.",
                        remedy=(
                            "CONFLUENCE_URL'nin Confluence'ın kendi adresi olduğundan "
                            "(Cloud'da .../wiki) ve token ile kimlik doğrulamaya izin "
                            "verildiğinden emin olun."
                        ),
                        status=status,
                    )
                raw = self._read_bounded(response)
                if status >= 400:
                    raise self._http_error(status, raw)
        except ConfluenceError:
            raise
        except requests.exceptions.SSLError as exc:
            raise ConfluenceError(
                "tls",
                f"Confluence'a güvenli bağlantı kurulamadı ({type(exc).__name__}).",
                remedy=_REMEDY_TLS,
            ) from None
        except requests.exceptions.ConnectTimeout:
            raise ConfluenceError(
                "unreachable",
                f"Confluence sunucusuna {self.config.connect_timeout_seconds:g} saniyede bağlanılamadı.",
                remedy=_REMEDY_NETWORK,
            ) from None
        except requests.exceptions.Timeout:
            raise ConfluenceError(
                "timeout", f"Confluence {self.config.timeout_seconds:g} saniyede yanıt vermedi."
            ) from None
        except requests.exceptions.ConnectionError as exc:
            if any(hint in str(exc) for hint in _NAME_RESOLUTION_HINTS):
                raise ConfluenceError(
                    "dns",
                    "Confluence sunucusunun adı çözümlenemedi "
                    f"({urlsplit(self.config.base_url).hostname}).",
                    remedy=_REMEDY_NETWORK,
                ) from None
            raise ConfluenceError(
                "unreachable", "Confluence sunucusuna bağlanılamadı.", remedy=_REMEDY_NETWORK
            ) from None
        except requests.exceptions.RequestException as exc:
            raise ConfluenceError(
                "network", f"Confluence isteği başarısız ({type(exc).__name__})."
            ) from None
        finally:
            _CALL_SLOTS.release()

        if not raw.strip():
            return {}
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            stabilization_logger.warning(
                "confluence: GET %s returned non-JSON (%.0f ms)",
                path,
                (time.monotonic() - started) * 1000,
            )
            raise ConfluenceError(
                "bad_output",
                "Confluence beklenen JSON biçiminde yanıt vermedi (bir giriş/proxy sayfası olabilir).",
                remedy=_REMEDY_NETWORK,
            ) from None

    @staticmethod
    def _read_bounded(response: Any) -> bytes:
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(64 * 1024):
            if not chunk:
                continue
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise ConfluenceError("too_large", "Confluence yanıtı çok büyük olduğu için okunamadı.")
        return b"".join(chunks)

    def _http_error(self, status: int, raw: bytes) -> ConfluenceError:
        says = _confluence_message(raw)
        suffix = f" Confluence: {says}" if says else ""
        if status == 401:
            return ConfluenceError(
                "auth", "Confluence kimlik bilgileri reddedildi (HTTP 401).",
                remedy=_REMEDY_AUTH, status=status,
            )
        if status == 403:
            return ConfluenceError(
                "forbidden",
                "Bu hesabın bu Confluence içeriğine erişim yetkisi yok (HTTP 403)." + suffix,
                status=status,
            )
        if status == 404:
            return ConfluenceError(
                "not_found", "Confluence içeriği bulunamadı veya erişim yok." + suffix, status=status
            )
        if status == 400:
            return ConfluenceError("bad_query", ("Confluence sorguyu reddetti." + suffix).strip(), status=status)
        if status == 429:
            return ConfluenceError(
                "rate_limited",
                "Confluence istekleri sınırladı (HTTP 429); bir süre sonra tekrar denenmeli.",
                status=status,
            )
        if status >= 500:
            return ConfluenceError("server", f"Confluence sunucu hatası (HTTP {status}).", status=status)
        return ConfluenceError(
            "failed", f"Confluence isteği başarısız (HTTP {status})." + suffix, status=status
        )

    # --- reads -------------------------------------------------------------

    def search(self, cql: str, *, limit: int = 25, with_body: bool = False) -> SearchPage:
        """Content matching ``cql``, which must already carry the space clause."""
        if build_space_clause(self.config.spaces) not in cql:
            # Belt and braces: a query that forgot the allowlist is a bug, and
            # the safe outcome of a bug here is no request at all.
            raise ConfluenceError("bad_request", "Confluence sorgusu space izin listesini içermiyor.")
        limit = max(1, min(int(limit), MAX_SEARCH_RESULTS))
        expand = "space,version,ancestors"
        if with_body:
            expand += ",body.storage"
        data = _as_dict(self._get(SEARCH_PATH, {"cql": cql, "limit": limit, "expand": expand}))
        results = [r for r in (data.get("results") or []) if isinstance(r, dict)]
        allowed = set(self.config.spaces)
        # Whatever the query said, nothing outside the allowlist leaves here.
        results = [r for r in results if space_key(r) in allowed]
        total = data.get("totalSize")
        total = total if isinstance(total, int) else None
        links = data.get("_links") if isinstance(data.get("_links"), dict) else {}
        truncated = bool(links.get("next")) or (total is not None and total > len(results))
        return SearchPage(results=results[:limit], total=total, truncated=truncated)

    def get_page(self, page_id: str) -> Optional[dict[str, Any]]:
        """One page or blog post with its body, only from an allowed space."""
        page_id = validate_page_id(page_id)
        page = self.search(
            f"id = {page_id} AND {build_space_clause(self.config.spaces)}",
            limit=1,
            with_body=True,
        )
        return page.results[0] if page.results else None

    def find_by_title(self, space: str, title: str) -> Optional[dict[str, Any]]:
        """``/display/SPACE/Title`` links (Server/DC): the page with that title."""
        if space not in self.config.spaces:
            return None
        page = self.search(
            f"space = {cql_string(space)} AND title = {cql_string(title)} "
            f"AND {build_space_clause(self.config.spaces)}",
            limit=1,
            with_body=True,
        )
        return page.results[0] if page.results else None

    def current_user(self) -> dict[str, Any]:
        return _as_dict(self._get("/rest/api/user/current", {}))


def _as_dict(data: Any) -> dict[str, Any]:
    return data if isinstance(data, dict) else {}


def space_key(content: Mapping[str, Any]) -> str:
    space = content.get("space") if isinstance(content.get("space"), dict) else {}
    return str(space.get("key") or "")


def validate_page_id(value: Any) -> str:
    text = str(value or "").strip()
    if not PAGE_ID_RE.match(text):
        raise ConfluenceError("bad_request", f"geçersiz Confluence sayfa kimliği: {text[:30]!r}")
    return text


# --------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------

CACHE_SECONDS = 60
_HEALTH_CACHE: dict[str, tuple[float, "HealthReport"]] = {}


@dataclass(frozen=True)
class HealthReport:
    enabled: bool = False
    configured: bool = False
    reachable: bool = False
    deployment: str = ""
    base_url: str = ""
    spaces: tuple[str, ...] = ()
    user: str = ""
    detail: str = ""
    remedy: str = ""
    error_code: str = ""

    @property
    def ok(self) -> bool:
        return self.configured and self.reachable

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "enabled": self.enabled,
            "configured": self.configured,
            "reachable": self.reachable,
            "deployment": self.deployment,
            "base_url": self.base_url,
            "spaces": list(self.spaces),
            "user": self.user,
            "detail": self.detail,
            "remedy": self.remedy,
            "error_code": self.error_code,
        }


def check_health(
    *, use_cache: bool = True, config: Optional[ConfluenceConfig] = None, session: Any = None
) -> HealthReport:
    """Can this deployment read Confluence right now? Never raises."""
    cfg = config or load_config()
    cache_key = f"{cfg.base_url}|{cfg.deployment}|{cfg.auth_mode}|{cfg.enabled}|{','.join(cfg.spaces)}"
    if use_cache:
        cached = _HEALTH_CACHE.get(cache_key)
        if cached and (time.time() - cached[0]) < CACHE_SECONDS:
            return cached[1]
    report = _probe(cfg, session)
    _HEALTH_CACHE[cache_key] = (time.time(), report)
    return report


def reset_health_cache() -> None:
    _HEALTH_CACHE.clear()


def _probe(cfg: ConfluenceConfig, session: Any) -> HealthReport:
    base = {
        "enabled": cfg.enabled,
        "deployment": cfg.deployment,
        "base_url": cfg.base_url,
        "spaces": cfg.spaces,
    }
    problems = cfg.problems()
    if problems:
        return HealthReport(
            **base,
            configured=False,
            detail=" ".join(problems),
            remedy="docs/confluence.md içindeki ayarları .env dosyasına girin.",
            error_code="not_configured",
        )
    try:
        me = ConfluenceClient(cfg, session=session).current_user()
    except ConfluenceError as exc:
        return HealthReport(
            **base, configured=True, detail=exc.detail, remedy=exc.remedy, error_code=exc.code
        )
    except Exception as exc:  # noqa: BLE001 - a broken probe is a report, not a crash
        return HealthReport(
            **base,
            configured=True,
            detail=f"Confluence durumu sorgulanamadı ({type(exc).__name__}).",
            error_code="failed",
        )
    name = str(me.get("displayName") or me.get("username") or me.get("publicName") or "")
    return HealthReport(
        **base,
        configured=True,
        reachable=True,
        user=name,
        detail=f"Bağlı ({name}); izinli space'ler: {', '.join(cfg.spaces)}."
        if name
        else f"Bağlı; izinli space'ler: {', '.join(cfg.spaces)}.",
    )
