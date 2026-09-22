"""Read-only Jira REST client for Jira Cloud and Jira Server / Data Center.

Why this is its own small client rather than ``atlassian-python-api`` or the
netresearch jira-skill CLIs
------------------------------------------------------------------------
Those were the reference (https://github.com/netresearch/jira-skill), and the
gotchas below come from them. But the library is pinned to API v2, whose
search endpoint Atlassian removed from Cloud (CHANGE-2046) -- a naive
``jira.jql()`` simply fails there -- and the skill's CLIs are written for an
agent with a shell, run through ``uv run`` with dependencies resolved at call
time. Here the model never calls tools: ``jira_controller`` runs a fixed
phase sequence, and this module is the only thing that talks to Jira.

What had to be right, and where it lives
----------------------------------------
* **Search differs by deployment.** Cloud: ``GET /rest/api/3/search/jql``
  with cursor paging (``nextPageToken``) and no ``total``. Server/DC:
  ``GET /rest/api/2/search`` with ``startAt``/``total``. See ``search``.
* **Issue and comment reads use API v2 on both.** v2 still exists on Cloud
  for these, and it returns description/comment bodies as wiki-markup
  strings rather than Cloud v3's ADF JSON trees -- text a model can read.
* **Embedded comments are truncated** (50 on Server/DC), so comments come
  from their own endpoint, newest page last (``recent_comments``).
* **Failures must say what fixes them.** The deployment this was written
  for reaches Jira over a company VPN, so "cannot resolve the name" and
  "connection timed out" are the common failures, and they are reported as
  such (``JiraError.code``) with a short connect timeout so a disconnected
  VPN costs seconds, not a minute.

Security notes
--------------
* **Read-only by construction.** Every request here is a GET except
  ``count`` (Cloud's approximate-count endpoint is POST-only and reads).
  There is no method that creates, edits, transitions or comments.
* **The host is fixed by configuration.** Paths are module constants plus
  validated issue keys (``ISSUE_KEY_RE``) that are URL-quoted; model output
  only ever reaches query-string values. ``_url`` refuses anything that
  would leave ``JIRA_URL``.
* **No redirects are followed.** A redirect from Jira's REST API is almost
  always an SSO login page; following it would at best parse HTML as JSON
  and at worst carry the credential to another host.
* **The token never appears in a log or an error.** ``JiraConfig`` keeps
  secrets out of ``repr`` and error details are built from status codes and
  Jira's own ``errorMessages``, never from request headers.
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
from urllib.parse import quote, urlsplit

stabilization_logger = logging.getLogger("agent_webapp.stabilization")

# Jira's own rule for keys: project key (letter first, then letters, digits
# or underscore), a dash, and a positive number.
ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,29}-[1-9][0-9]{0,8}$")

DEFAULT_TIMEOUT_SECONDS = 20.0
# Short on purpose: the common failure on a VPN-only Jira is "not connected",
# and a job should learn that in seconds rather than wait out a read timeout.
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
# Upper bound for any one search, whatever the caller asks for.
MAX_SEARCH_RESULTS = 100
MAX_SEARCH_PAGES = 5
MAX_ERROR_CHARS = 400

# Across the whole backend. Jira rate-limits per account, and several jobs
# reading at once all use the same account.
MAX_CONCURRENT_CALLS = int(os.environ.get("JIRA_MAX_CONCURRENT") or 4)
MAX_QUEUE_WAIT_SECONDS = 60
_CALL_SLOTS = threading.BoundedSemaphore(max(1, MAX_CONCURRENT_CALLS))

_USER_AGENT = "agent-webapp/1.0 (+jira read-only)"

SEARCH_FIELDS = (
    "summary",
    "status",
    "assignee",
    "reporter",
    "priority",
    "issuetype",
    "created",
    "updated",
    "duedate",
    "resolution",
)
DETAIL_FIELDS = SEARCH_FIELDS + (
    "description",
    "labels",
    "components",
    "fixVersions",
    "parent",
    "issuelinks",
)

# Codes that mean "nothing further will work in this run", as opposed to one
# unreadable issue. The controller stops at these instead of trying the next
# issue and failing the same way five more times.
FATAL_CODES = frozenset(
    {
        "not_configured",
        "dns",
        "unreachable",
        "tls",
        "auth",
        "captcha",
        "redirect",
        "busy",
    }
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


@dataclass(frozen=True)
class JiraConfig:
    enabled: bool = False
    base_url: str = ""
    # "cloud" | "server". Server covers Data Center too: same API.
    deployment: str = "server"
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
        """"pat" (Server/DC bearer token), "basic" (email/user + token), or ""."""
        if self.personal_token and not self.is_cloud:
            return "pat"
        if self.username and self.api_token:
            return "basic"
        return ""

    def problems(self) -> list[str]:
        """What stops this configuration from working, in words a user can act on."""
        found: list[str] = []
        if not self.enabled:
            found.append("JIRA_ENABLED kapalı.")
        if not self.base_url:
            found.append("JIRA_URL boş.")
        else:
            parts = urlsplit(self.base_url)
            if parts.scheme not in {"http", "https"} or not parts.hostname:
                found.append("JIRA_URL http:// veya https:// ile başlayan tam bir adres olmalı.")
            elif parts.query or parts.fragment:
                found.append("JIRA_URL sorgu veya # içermemeli.")
        if not self.auth_mode:
            if self.is_cloud:
                found.append(
                    "Jira Cloud için JIRA_USERNAME (e-posta) ve JIRA_API_TOKEN gerekli."
                )
            else:
                found.append(
                    "Jira Server/Data Center için JIRA_PERSONAL_TOKEN "
                    "(veya JIRA_USERNAME + JIRA_API_TOKEN) gerekli."
                )
        return found

    @property
    def configured(self) -> bool:
        return not self.problems()

    def browse_url(self, key: str) -> str:
        return f"{self.base_url}/browse/{quote(key, safe='')}"


def _detect_deployment(base_url: str) -> str:
    host = (urlsplit(base_url).hostname or "").lower()
    if host.endswith(".atlassian.net") or host.endswith(".jira.com"):
        return "cloud"
    return "server"


def load_config(environ: Optional[Mapping[str, str]] = None) -> JiraConfig:
    """Read the ``JIRA_*`` settings. Read on every call, never cached, so a
    changed ``.env`` takes effect with a restart and tests can pass their own."""
    env = os.environ if environ is None else environ
    base_url = str(env.get("JIRA_URL") or "").strip().rstrip("/")
    deployment = str(env.get("JIRA_DEPLOYMENT") or "auto").strip().lower()
    if deployment not in {"cloud", "server"}:
        deployment = _detect_deployment(base_url)
    return JiraConfig(
        enabled=_flag(env, "JIRA_ENABLED", False),
        base_url=base_url,
        deployment=deployment,
        username=str(env.get("JIRA_USERNAME") or "").strip(),
        api_token=str(env.get("JIRA_API_TOKEN") or "").strip(),
        personal_token=str(env.get("JIRA_PERSONAL_TOKEN") or "").strip(),
        timeout_seconds=_float(
            env, "JIRA_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS, low=3, high=120
        ),
        connect_timeout_seconds=_float(
            env,
            "JIRA_CONNECT_TIMEOUT_SECONDS",
            DEFAULT_CONNECT_TIMEOUT_SECONDS,
            low=1,
            high=60,
        ),
    )


def is_enabled() -> bool:
    return load_config().enabled


def is_configured() -> bool:
    """Enabled and complete. Deliberately NOT a reachability check -- see
    ``main._routing_for_workforce`` for why routing must not probe the network."""
    return load_config().configured


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class JiraError(Exception):
    """A failed Jira call, classified by what would fix it."""

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
    "Jira şirket ağından/VPN'den erişilebiliyorsa VPN bağlantısını kontrol edin; "
    "JIRA_URL'nin doğru olduğundan emin olun (docs/jira.md)."
)
_REMEDY_AUTH_CLOUD = (
    "Jira Cloud: JIRA_USERNAME hesabın e-posta adresi, JIRA_API_TOKEN "
    "id.atlassian.com'da üretilmiş API token olmalı."
)
_REMEDY_AUTH_SERVER = (
    "Jira Server/Data Center: profilinizden bir Personal Access Token üretip "
    "JIRA_PERSONAL_TOKEN olarak girin."
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


def _jira_messages(raw: bytes) -> str:
    """Jira's own explanation from an error body, when it sent JSON."""
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, AttributeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    messages = [str(m) for m in payload.get("errorMessages") or [] if m]
    errors = payload.get("errors")
    if isinstance(errors, dict):
        messages += [f"{k}: {v}" for k, v in errors.items() if v]
    return " ".join(messages)[:MAX_ERROR_CHARS]


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SearchPage:
    issues: list[dict[str, Any]]
    # Server/DC reports an exact total; Cloud's search does not (see count).
    total: Optional[int] = None
    # More results existed than were fetched.
    truncated: bool = False


class JiraClient:
    """One configured Jira. ``session`` is injectable for tests."""

    def __init__(self, config: JiraConfig, *, session: Any = None) -> None:
        self.config = config
        if session is None:
            import requests

            session = requests.Session()
        self._session = session

    # --- transport ---------------------------------------------------------

    def _url(self, path: str) -> str:
        if not path.startswith("/rest/"):
            raise JiraError("bad_request", "geçersiz Jira yolu")
        url = self.config.base_url + path
        # Cheap proof that no value reached the host part: the URL must still
        # begin with the configured base followed by the path separator.
        if not url.startswith(self.config.base_url + "/rest/"):
            raise JiraError("bad_request", "Jira isteği yapılandırılan adresin dışına çıkıyor")
        return url

    def _headers(self, *, json_body: bool = False) -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": _USER_AGENT}
        if json_body:
            headers["Content-Type"] = "application/json"
        if self.config.auth_mode == "pat":
            headers["Authorization"] = f"Bearer {self.config.personal_token}"
        return headers

    def _auth(self) -> Optional[tuple[str, str]]:
        if self.config.auth_mode == "basic":
            return (self.config.username, self.config.api_token)
        return None

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        body: Optional[dict[str, Any]] = None,
    ) -> Any:
        if not self.config.configured:
            raise JiraError(
                "not_configured",
                "Jira yapılandırılmamış: " + " ".join(self.config.problems()),
                remedy="docs/jira.md içindeki ayarları .env dosyasına girin.",
            )
        import requests

        url = self._url(path)
        if not _CALL_SLOTS.acquire(timeout=MAX_QUEUE_WAIT_SECONDS):
            raise JiraError(
                "busy",
                f"Eşzamanlı Jira isteği sınırı ({MAX_CONCURRENT_CALLS}) dolu ve sıra açılmadı.",
            )
        started = time.monotonic()
        try:
            with self._session.request(
                method,
                url,
                params=params,
                data=json.dumps(body) if body is not None else None,
                headers=self._headers(json_body=body is not None),
                auth=self._auth(),
                timeout=(self.config.connect_timeout_seconds, self.config.timeout_seconds),
                allow_redirects=False,
                stream=True,
            ) as response:
                status = int(response.status_code)
                if 300 <= status < 400:
                    raise JiraError(
                        "redirect",
                        f"Jira isteği başka bir adrese yönlendirdi (HTTP {status}); "
                        "büyük ihtimalle bir SSO giriş sayfası.",
                        remedy=(
                            "JIRA_URL'nin Jira'nın kendi adresi olduğundan ve token ile "
                            "kimlik doğrulamaya izin verildiğinden emin olun."
                        ),
                        status=status,
                    )
                raw = self._read_bounded(response)
                if status >= 400:
                    raise self._http_error(status, response.headers, raw)
        except JiraError:
            raise
        except requests.exceptions.SSLError as exc:
            raise JiraError(
                "tls", f"Jira'ya güvenli bağlantı kurulamadı ({type(exc).__name__}).",
                remedy=_REMEDY_TLS,
            ) from None
        except requests.exceptions.ConnectTimeout:
            raise JiraError(
                "unreachable",
                f"Jira sunucusuna {self.config.connect_timeout_seconds:g} saniyede "
                "bağlanılamadı.",
                remedy=_REMEDY_NETWORK,
            ) from None
        except requests.exceptions.Timeout:
            raise JiraError(
                "timeout",
                f"Jira {self.config.timeout_seconds:g} saniyede yanıt vermedi.",
            ) from None
        except requests.exceptions.ConnectionError as exc:
            text = str(exc)
            if any(hint in text for hint in _NAME_RESOLUTION_HINTS):
                raise JiraError(
                    "dns",
                    "Jira sunucusunun adı çözümlenemedi "
                    f"({urlsplit(self.config.base_url).hostname}).",
                    remedy=_REMEDY_NETWORK,
                ) from None
            raise JiraError(
                "unreachable", "Jira sunucusuna bağlanılamadı.", remedy=_REMEDY_NETWORK
            ) from None
        except requests.exceptions.RequestException as exc:
            raise JiraError(
                "network", f"Jira isteği başarısız ({type(exc).__name__})."
            ) from None
        finally:
            _CALL_SLOTS.release()

        if not raw.strip():
            return {}
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            stabilization_logger.warning(
                "jira: %s %s returned non-JSON (%.0f ms)",
                method,
                path,
                (time.monotonic() - started) * 1000,
            )
            raise JiraError(
                "bad_output",
                "Jira beklenen JSON biçiminde yanıt vermedi (bir giriş/proxy sayfası olabilir).",
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
                raise JiraError("too_large", "Jira yanıtı çok büyük olduğu için okunamadı.")
        return b"".join(chunks)

    def _http_error(self, status: int, headers: Mapping[str, str], raw: bytes) -> JiraError:
        jira_says = _jira_messages(raw)
        suffix = f" Jira: {jira_says}" if jira_says else ""
        # Server/DC locks an account behind a CAPTCHA after failed logins and
        # then refuses every API call with this header until someone logs in
        # through the browser once -- a different fix from a bad token.
        login_reason = str((headers or {}).get("X-Seraph-LoginReason") or "")
        if "AUTHENTICATION_DENIED" in login_reason:
            return JiraError(
                "captcha",
                "Jira hesabı CAPTCHA doğrulamasına düştü; API istekleri reddediliyor.",
                remedy="Aynı hesapla tarayıcıdan bir kez Jira'ya giriş yapıp CAPTCHA'yı çözün.",
                status=status,
            )
        if status == 401:
            return JiraError(
                "auth",
                "Jira kimlik bilgileri reddedildi (HTTP 401).",
                remedy=_REMEDY_AUTH_CLOUD if self.config.is_cloud else _REMEDY_AUTH_SERVER,
                status=status,
            )
        if status == 403:
            return JiraError(
                "forbidden",
                "Bu Jira hesabının bu içeriğe erişim yetkisi yok (HTTP 403)." + suffix,
                status=status,
            )
        if status == 404:
            return JiraError(
                "not_found", "Aranan Jira kaydı bulunamadı veya erişim yok." + suffix,
                status=status,
            )
        if status == 400:
            return JiraError(
                "bad_query", ("Jira sorguyu reddetti." + suffix).strip(), status=status
            )
        if status == 429:
            return JiraError(
                "rate_limited",
                "Jira istekleri sınırladı (HTTP 429); bir süre sonra tekrar denenmeli.",
                status=status,
            )
        if status >= 500:
            return JiraError(
                "server", f"Jira sunucu hatası (HTTP {status}).", status=status
            )
        return JiraError("failed", f"Jira isteği başarısız (HTTP {status})." + suffix, status=status)

    # --- reads -------------------------------------------------------------

    def myself(self) -> dict[str, Any]:
        data = self._request("GET", "/rest/api/2/myself")
        return data if isinstance(data, dict) else {}

    def search(
        self,
        jql: str,
        *,
        limit: int = 50,
        fields: tuple[str, ...] = SEARCH_FIELDS,
    ) -> SearchPage:
        """Issues matching ``jql``, up to ``limit``. Pages internally."""
        limit = max(1, min(int(limit), MAX_SEARCH_RESULTS))
        issues: list[dict[str, Any]] = []
        field_list = ",".join(fields)
        if self.config.is_cloud:
            token = ""
            for _ in range(MAX_SEARCH_PAGES):
                params: dict[str, Any] = {
                    "jql": jql,
                    "fields": field_list,
                    "maxResults": min(100, limit - len(issues)),
                }
                if token:
                    params["nextPageToken"] = token
                data = _as_dict(self._request("GET", "/rest/api/3/search/jql", params=params))
                page = [i for i in (data.get("issues") or []) if isinstance(i, dict)]
                issues.extend(page)
                token = str(data.get("nextPageToken") or "")
                is_last = bool(data.get("isLast", not token))
                if is_last or not token or not page or len(issues) >= limit:
                    return SearchPage(
                        issues=issues[:limit],
                        total=None,
                        truncated=(not is_last and bool(token)) or len(issues) > limit,
                    )
            return SearchPage(issues=issues[:limit], total=None, truncated=True)

        start = 0
        total: Optional[int] = None
        for _ in range(MAX_SEARCH_PAGES):
            data = _as_dict(self._request(
                "GET",
                "/rest/api/2/search",
                params={
                    "jql": jql,
                    "fields": field_list,
                    "maxResults": min(100, limit - len(issues)),
                    "startAt": start,
                },
            ))
            page = [i for i in (data.get("issues") or []) if isinstance(i, dict)]
            raw_total = data.get("total")
            total = raw_total if isinstance(raw_total, int) else total
            issues.extend(page)
            start += len(page)
            if not page or len(issues) >= limit or (total is not None and start >= total):
                break
        return SearchPage(
            issues=issues[:limit],
            total=total,
            truncated=total is not None and total > min(len(issues), limit),
        )

    def count(self, jql: str) -> Optional[int]:
        """How many issues match, when Jira can say.

        Cloud only: its search returns no total, and "kaç tane açık bug var"
        is a question people actually ask. Server/DC already gets the exact
        figure from ``search``, so this returns None there.
        """
        if not self.config.is_cloud:
            return None
        data = self._request(
            "POST", "/rest/api/3/search/approximate-count", body={"jql": jql}
        )
        value = data.get("count") if isinstance(data, dict) else None
        return value if isinstance(value, int) else None

    def get_issue(
        self, key: str, *, fields: tuple[str, ...] = DETAIL_FIELDS
    ) -> dict[str, Any]:
        key = validate_issue_key(key)
        data = self._request(
            "GET",
            f"/rest/api/2/issue/{quote(key, safe='')}",
            params={"fields": ",".join(fields)},
        )
        return data if isinstance(data, dict) else {}

    def recent_comments(self, key: str, *, limit: int = 5) -> tuple[list[dict[str, Any]], int]:
        """The newest ``limit`` comments, oldest first, and the total count.

        Two calls at most: one to learn the total, one for the last page. The
        embedded ``fields.comment`` block is not used because Jira truncates it.
        """
        key = validate_issue_key(key)
        limit = max(1, min(int(limit), 50))
        path = f"/rest/api/2/issue/{quote(key, safe='')}/comment"
        first = _as_dict(self._request("GET", path, params={"startAt": 0, "maxResults": limit}))
        comments = [c for c in (first.get("comments") or []) if isinstance(c, dict)]
        raw_total = first.get("total")
        total = raw_total if isinstance(raw_total, int) else len(comments)
        if total > limit:
            last = _as_dict(self._request(
                "GET", path, params={"startAt": total - limit, "maxResults": limit}
            ))
            comments = [c for c in (last.get("comments") or []) if isinstance(c, dict)]
        return comments[-limit:], total

    def find_users(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        """People matching a name, for "Ahmet'e atanan işler".

        Cloud searches with ``query`` and identifies people by accountId
        (usernames were removed for privacy); Server/DC takes ``username``
        and identifies them by ``name``.
        """
        text = str(query or "").strip()[:80]
        if not text:
            return []
        param = "query" if self.config.is_cloud else "username"
        data = self._request(
            "GET",
            "/rest/api/2/user/search",
            params={param: text, "maxResults": max(1, min(int(limit), 20))},
        )
        return [u for u in data if isinstance(u, dict)] if isinstance(data, list) else []


def _as_dict(data: Any) -> dict[str, Any]:
    return data if isinstance(data, dict) else {}


def validate_issue_key(value: Any) -> str:
    key = str(value or "").strip().upper()
    if not ISSUE_KEY_RE.match(key):
        raise JiraError("bad_request", f"geçersiz Jira anahtarı: {key[:40]!r}")
    return key


def user_literal(user: Mapping[str, Any], *, cloud: bool) -> str:
    """The value JQL expects for a person: accountId on Cloud, name on Server/DC."""
    if cloud:
        return str(user.get("accountId") or "")
    return str(user.get("name") or user.get("key") or "")


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
            "user": self.user,
            "detail": self.detail,
            "remedy": self.remedy,
            "error_code": self.error_code,
        }


def check_health(
    *, use_cache: bool = True, config: Optional[JiraConfig] = None, session: Any = None
) -> HealthReport:
    """Can this deployment read Jira right now? Never raises."""
    cfg = config or load_config()
    cache_key = f"{cfg.base_url}|{cfg.deployment}|{cfg.auth_mode}|{cfg.enabled}"
    if use_cache:
        cached = _HEALTH_CACHE.get(cache_key)
        if cached and (time.time() - cached[0]) < CACHE_SECONDS:
            return cached[1]
    report = _probe(cfg, session)
    _HEALTH_CACHE[cache_key] = (time.time(), report)
    return report


def reset_health_cache() -> None:
    _HEALTH_CACHE.clear()


def _probe(cfg: JiraConfig, session: Any) -> HealthReport:
    base = {"enabled": cfg.enabled, "deployment": cfg.deployment, "base_url": cfg.base_url}
    problems = cfg.problems()
    if problems:
        return HealthReport(
            **base,
            configured=False,
            detail=" ".join(problems),
            remedy="docs/jira.md içindeki ayarları .env dosyasına girin.",
            error_code="not_configured",
        )
    try:
        me = JiraClient(cfg, session=session).myself()
    except JiraError as exc:
        return HealthReport(
            **base,
            configured=True,
            detail=exc.detail,
            remedy=exc.remedy,
            error_code=exc.code,
        )
    except Exception as exc:  # noqa: BLE001 - a broken probe is a report, not a crash
        return HealthReport(
            **base,
            configured=True,
            detail=f"Jira durumu sorgulanamadı ({type(exc).__name__}).",
            error_code="failed",
        )
    name = str(me.get("displayName") or me.get("name") or me.get("emailAddress") or "")
    return HealthReport(
        **base,
        configured=True,
        reachable=True,
        user=name,
        detail=f"Bağlı ({name})." if name else "Bağlı.",
    )
