"""Read-only GitHub REST client.

Why the REST API directly, and not the ``gh`` CLI Agent-Reach uses
-----------------------------------------------------------------
Agent-Reach (github.com/Panniantong/Agent-Reach) reads GitHub by installing
``gh`` and letting an agent shell out to it. Here the model never calls
tools -- ``github_controller`` runs a fixed phase sequence -- so a CLI would
add a binary to the image and an argv boundary to defend (``social.registry``
shows what that costs) for no gain: every read below is one GET against a
public JSON API. The same shape as ``jira_client``, and the Hacker News
reader in ``social``.

What had to be right, and where it lives
----------------------------------------
* **Rate limits are the real constraint.** Anonymous: 60 requests an hour
  and 10 searches a minute, per IP, shared by every user of this backend.
  With ``GITHUB_TOKEN`` (a fine-grained token with no permissions is
  enough for public repositories): 5,000 an hour and 30 searches a minute.
  Exhaustion is its own error code with the reset time and the fix
  (``_http_error``), and ``last_rate`` keeps the latest headers for the
  status endpoint.
* **Conditional requests are free.** A 304 does not count against the
  limit, so responses are cached with their ETag (``_CACHE``): the same
  repository read again in a follow-up turn costs nothing while fresh and
  a free revalidation after that.
* **Renamed repositories redirect.** GitHub answers a renamed or
  transferred repository with 301 to ``/repositories/<id>``. That is
  followed -- but only on the configured API host, never elsewhere
  (``_request``), so a redirect cannot carry the token to another origin.

Security notes
--------------
* **Read-only by construction.** Every request is a GET. There is no
  method that stars, forks, comments, or opens anything, and
  ``test_github_client`` asserts that no other verb can be sent.
* **The host is fixed by configuration.** Paths are built here from values
  ``github_query`` validated, each segment URL-quoted; model output only
  ever reaches the ``q`` query-string value of the search endpoint.
* **The token never appears in a log or an error.** ``GitHubConfig`` keeps
  it out of ``repr``, and error details are built from status codes and
  GitHub's own ``message`` field, never from request headers.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional
from urllib.parse import quote, urlsplit

import github_query

stabilization_logger = logging.getLogger("agent_webapp.stabilization")

DEFAULT_API_URL = "https://api.github.com"
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
# A file is read up to this many bytes; the rest is cut, not refused. A
# 3 MB generated file is still worth its first screenful.
MAX_FILE_BYTES = 512 * 1024
MAX_README_BYTES = 256 * 1024
MAX_SEARCH_RESULTS = 50
MAX_TREE_ENTRIES = 400
MAX_ERROR_CHARS = 300
MAX_REDIRECTS = 2

MAX_CONCURRENT_CALLS = int(os.environ.get("GITHUB_MAX_CONCURRENT") or 4)
MAX_QUEUE_WAIT_SECONDS = 60
_CALL_SLOTS = threading.BoundedSemaphore(max(1, MAX_CONCURRENT_CALLS))

# Response cache: fresh entries are served without a request; stale ones are
# revalidated with If-None-Match (a 304 is not charged against the limit).
CACHE_FRESH_SECONDS = 600
CACHE_MAX_ENTRIES = 256
_CACHE: "OrderedDict[str, tuple[float, str, bytes]]" = OrderedDict()
_CACHE_LOCK = threading.Lock()

_USER_AGENT = "agent-webapp/1.0 (+github read-only)"
_API_VERSION = "2022-11-28"
ACCEPT_JSON = "application/vnd.github+json"
ACCEPT_RAW = "application/vnd.github.raw+json"

# Codes that mean "nothing further will work in this run". The controller
# stops at these instead of failing the same way for every remaining repo.
FATAL_CODES = frozenset(
    {"not_configured", "dns", "unreachable", "tls", "auth", "rate_limited", "busy"}
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
class GitHubConfig:
    # On by default, unlike Jira or Outlook: public GitHub needs no account,
    # and reading it is no different in kind from the web research this
    # stack already does.
    enabled: bool = True
    api_url: str = DEFAULT_API_URL
    token: str = field(default="", repr=False)
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS

    @property
    def authenticated(self) -> bool:
        return bool(self.token)

    @property
    def web_url(self) -> str:
        """Where html links point. github.com for the public API; for an
        Enterprise Server ``https://host/api/v3`` it is ``https://host``."""
        if self.api_url.rstrip("/") == DEFAULT_API_URL:
            return "https://github.com"
        parts = urlsplit(self.api_url)
        return f"{parts.scheme}://{parts.netloc}"

    def problems(self) -> list[str]:
        found: list[str] = []
        if not self.enabled:
            found.append("GITHUB_ENABLED kapalı.")
        parts = urlsplit(self.api_url)
        if parts.scheme != "https" or not parts.hostname:
            found.append("GITHUB_API_URL https:// ile başlayan tam bir adres olmalı.")
        elif parts.query or parts.fragment:
            found.append("GITHUB_API_URL sorgu veya # içermemeli.")
        return found

    @property
    def configured(self) -> bool:
        return not self.problems()


def load_config(environ: Optional[Mapping[str, str]] = None) -> GitHubConfig:
    """Read the ``GITHUB_*`` settings on every call, never cached."""
    env = os.environ if environ is None else environ
    return GitHubConfig(
        enabled=_flag(env, "GITHUB_ENABLED", True),
        api_url=str(env.get("GITHUB_API_URL") or DEFAULT_API_URL).strip().rstrip("/"),
        token=str(env.get("GITHUB_TOKEN") or "").strip(),
        timeout_seconds=_float(env, "GITHUB_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS, low=3, high=120),
        connect_timeout_seconds=_float(
            env, "GITHUB_CONNECT_TIMEOUT_SECONDS", DEFAULT_CONNECT_TIMEOUT_SECONDS, low=1, high=60
        ),
    )


def is_configured() -> bool:
    """Enabled and well-formed. Not a reachability check (see main)."""
    return load_config().configured


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class GitHubError(Exception):
    """A failed GitHub call, classified by what would fix it."""

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


_REMEDY_TOKEN = (
    "Daha yüksek sınır için .env dosyasına GITHUB_TOKEN ekleyin (izin "
    "gerektirmeyen bir fine-grained token yeterli; docs/github.md)."
)
_REMEDY_BAD_TOKEN = (
    "GITHUB_TOKEN geçersiz veya süresi dolmuş. github.com/settings/personal-access-tokens "
    "adresinden yenisini üretin ya da satırı boşaltın (docs/github.md)."
)
_REMEDY_NETWORK = "Sunucunun internete (api.github.com) erişebildiğini kontrol edin."

_NAME_RESOLUTION_HINTS = (
    "NameResolutionError",
    "Name or service not known",
    "nodename nor servname",
    "getaddrinfo failed",
    "Temporary failure in name resolution",
    "No address associated with hostname",
)


@dataclass(frozen=True)
class RateInfo:
    limit: Optional[int] = None
    remaining: Optional[int] = None
    reset_epoch: Optional[int] = None
    resource: str = ""

    def reset_in_minutes(self, now: Optional[float] = None) -> Optional[int]:
        if self.reset_epoch is None:
            return None
        seconds = self.reset_epoch - (now if now is not None else time.time())
        return max(1, int(seconds // 60) + 1)


def _int_header(headers: Mapping[str, str], name: str) -> Optional[int]:
    try:
        return int(str((headers or {}).get(name) or "").strip())
    except ValueError:
        return None


def _rate_from(headers: Mapping[str, str]) -> RateInfo:
    return RateInfo(
        limit=_int_header(headers, "X-RateLimit-Limit"),
        remaining=_int_header(headers, "X-RateLimit-Remaining"),
        reset_epoch=_int_header(headers, "X-RateLimit-Reset"),
        resource=str((headers or {}).get("X-RateLimit-Resource") or ""),
    )


def _github_message(raw: bytes) -> str:
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, AttributeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    message = str(payload.get("message") or "")
    errors = payload.get("errors")
    if isinstance(errors, list):
        extra = [str(e.get("message") or e.get("code") or "") for e in errors if isinstance(e, dict)]
        message = " ".join([message] + [e for e in extra if e])
    return message.strip()[:MAX_ERROR_CHARS]


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SearchPage:
    items: list[dict[str, Any]]
    total: int = 0
    # GitHub stopped searching early (a timeout on its side); results partial.
    incomplete: bool = False


@dataclass(frozen=True)
class FileContent:
    path: str
    size: int
    text: str = ""
    truncated: bool = False
    binary: bool = False
    html_url: str = ""


@dataclass(frozen=True)
class TreeListing:
    # (path, "file" | "dir", size) for each entry, directories first.
    entries: list[tuple[str, str, int]]
    truncated: bool = False


def _decode_text(data: bytes) -> tuple[str, bool]:
    """(text, is_binary). NUL bytes near the start are the usual tell."""
    if b"\x00" in data[:8192]:
        return "", True
    try:
        return data.decode("utf-8"), False
    except UnicodeDecodeError:
        text = data.decode("utf-8", "replace")
        # Mostly-garbage decoding is binary in practice.
        if text[:4096].count("�") > 40:
            return "", True
        return text, False


class GitHubClient:
    """One configured GitHub. ``session`` is injectable for tests."""

    last_rate: Optional[RateInfo] = None

    def __init__(self, config: GitHubConfig, *, session: Any = None, use_cache: bool = True) -> None:
        self.config = config
        if session is None:
            import requests

            session = requests.Session()
        self._session = session
        self._use_cache = use_cache
        self._api_host = (urlsplit(config.api_url).netloc or "").lower()

    # --- transport ---------------------------------------------------------

    def _url(self, path: str) -> str:
        if not path.startswith("/") or path.startswith("//"):
            raise GitHubError("bad_request", "geçersiz GitHub yolu")
        url = self.config.api_url + path
        parts = urlsplit(url)
        if parts.netloc.lower() != self._api_host:
            raise GitHubError("bad_request", "GitHub isteği yapılandırılan adresin dışına çıkıyor")
        return url

    def _headers(self, accept: str, etag: str = "") -> dict[str, str]:
        headers = {
            "Accept": accept,
            "User-Agent": _USER_AGENT,
            "X-GitHub-Api-Version": _API_VERSION,
        }
        if self.config.token:
            headers["Authorization"] = f"Bearer {self.config.token}"
        if etag:
            headers["If-None-Match"] = etag
        return headers

    def _cache_key(self, url: str, params: Optional[dict[str, Any]], accept: str) -> str:
        items = sorted((params or {}).items())
        # The token is part of the key only as a yes/no: an anonymous and an
        # authenticated read of a public page are the same content, but a
        # 404 for one may not be a 404 for the other.
        return json.dumps([url, items, accept, bool(self.config.token)], default=str)

    def _request(
        self,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        accept: str = ACCEPT_JSON,
        max_bytes: int = MAX_RESPONSE_BYTES,
        truncate: bool = False,
    ) -> tuple[bytes, bool]:
        """GET ``path``. Returns (body, was_truncated). The only verb there is."""
        if not self.config.configured:
            raise GitHubError(
                "not_configured",
                "GitHub okuma kapalı: " + " ".join(self.config.problems()),
                remedy="docs/github.md",
            )
        url = self._url(path)
        key = self._cache_key(url, params, accept)
        cached = self._cache_get(key) if self._use_cache else None
        if cached is not None and time.time() - cached[0] < CACHE_FRESH_SECONDS:
            return cached[2], False

        if not _CALL_SLOTS.acquire(timeout=MAX_QUEUE_WAIT_SECONDS):
            raise GitHubError(
                "busy", f"Eşzamanlı GitHub isteği sınırı ({MAX_CONCURRENT_CALLS}) dolu."
            )
        try:
            body, truncated, etag = self._send(
                url, params, accept, cached[1] if cached else "", max_bytes, truncate
            )
        finally:
            _CALL_SLOTS.release()
        if body is None and cached is not None:
            # 304: unchanged. Refresh the entry's age; the body is the cached one.
            self._cache_put(key, cached[1], cached[2])
            return cached[2], False
        assert body is not None
        if self._use_cache and etag and not truncated:
            self._cache_put(key, etag, body)
        return body, truncated

    def _send(
        self,
        url: str,
        params: Optional[dict[str, Any]],
        accept: str,
        etag: str,
        max_bytes: int,
        truncate: bool,
    ) -> tuple[Optional[bytes], bool, str]:
        import requests

        target = url
        query = params
        for _ in range(MAX_REDIRECTS + 1):
            try:
                with self._session.request(
                    "GET",
                    target,
                    params=query,
                    headers=self._headers(accept, etag),
                    timeout=(self.config.connect_timeout_seconds, self.config.timeout_seconds),
                    allow_redirects=False,
                    stream=True,
                ) as response:
                    status = int(response.status_code)
                    headers = response.headers or {}
                    if headers.get("X-RateLimit-Remaining") is not None:
                        GitHubClient.last_rate = _rate_from(headers)
                    if status == 304:
                        return None, False, etag
                    if status in {301, 302, 307, 308}:
                        location = str(headers.get("Location") or "")
                        parts = urlsplit(location)
                        if parts.scheme != "https" or parts.netloc.lower() != self._api_host:
                            raise GitHubError(
                                "redirect",
                                "GitHub isteği yapılandırılan adresin dışına yönlendirdi.",
                                status=status,
                            )
                        # The Location already carries its own query string.
                        target, query = location, None
                        continue
                    raw, truncated = self._read_bounded(response, max_bytes, truncate)
                    if status >= 400:
                        raise self._http_error(status, headers, raw)
                    return raw, truncated, str(headers.get("ETag") or "")
            except GitHubError:
                raise
            except requests.exceptions.SSLError as exc:
                raise GitHubError(
                    "tls", f"GitHub'a güvenli bağlantı kurulamadı ({type(exc).__name__})."
                ) from None
            except requests.exceptions.ConnectTimeout:
                raise GitHubError(
                    "unreachable", "GitHub'a bağlanılamadı (zaman aşımı).", remedy=_REMEDY_NETWORK
                ) from None
            except requests.exceptions.Timeout:
                raise GitHubError(
                    "timeout", f"GitHub {self.config.timeout_seconds:g} saniyede yanıt vermedi."
                ) from None
            except requests.exceptions.ConnectionError as exc:
                if any(hint in str(exc) for hint in _NAME_RESOLUTION_HINTS):
                    raise GitHubError(
                        "dns", "GitHub sunucusunun adı çözümlenemedi.", remedy=_REMEDY_NETWORK
                    ) from None
                raise GitHubError(
                    "unreachable", "GitHub'a bağlanılamadı.", remedy=_REMEDY_NETWORK
                ) from None
            except requests.exceptions.RequestException as exc:
                raise GitHubError(
                    "network", f"GitHub isteği başarısız ({type(exc).__name__})."
                ) from None
        raise GitHubError("redirect", "GitHub çok fazla yönlendirme yaptı.")

    @staticmethod
    def _read_bounded(response: Any, limit: int, truncate: bool) -> tuple[bytes, bool]:
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(64 * 1024):
            if not chunk:
                continue
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                if truncate:
                    return b"".join(chunks)[:limit], True
                raise GitHubError("too_large", "GitHub yanıtı çok büyük olduğu için okunamadı.")
        return b"".join(chunks), False

    def _http_error(self, status: int, headers: Mapping[str, str], raw: bytes) -> GitHubError:
        message = _github_message(raw)
        suffix = f" GitHub: {message}" if message else ""
        rate = _rate_from(headers)
        rate_hit = status == 429 or (
            status == 403
            and (rate.remaining == 0 or "rate limit" in message.casefold())
        )
        if rate_hit:
            minutes = rate.reset_in_minutes()
            retry_after = _int_header(headers, "Retry-After")
            if minutes is None and retry_after:
                minutes = max(1, retry_after // 60 + 1)
            when = f" Yaklaşık {minutes} dakika sonra açılır." if minutes else ""
            scope = "arama" if rate.resource == "search" else "istek"
            return GitHubError(
                "rate_limited",
                f"GitHub {scope} sınırı doldu.{when}",
                remedy="" if self.config.authenticated else _REMEDY_TOKEN,
                status=status,
            )
        if status == 401:
            return GitHubError("auth", "GitHub token'ı reddedildi (HTTP 401).",
                               remedy=_REMEDY_BAD_TOKEN, status=status)
        if status == 403:
            return GitHubError("forbidden", "Bu GitHub içeriğine erişim yok (HTTP 403)." + suffix,
                               status=status)
        if status == 404:
            return GitHubError("not_found", "GitHub'da bulunamadı (depo özel, silinmiş veya adı yanlış).",
                               status=status)
        if status == 451:
            return GitHubError("blocked", "Bu depo yasal nedenle erişime kapalı (HTTP 451).",
                               status=status)
        if status == 422:
            return GitHubError("bad_query", ("GitHub sorguyu reddetti." + suffix).strip(),
                               status=status)
        if status >= 500:
            return GitHubError("server", f"GitHub sunucu hatası (HTTP {status}).", status=status)
        return GitHubError("failed", f"GitHub isteği başarısız (HTTP {status})." + suffix,
                           status=status)

    def _json(self, path: str, **params: Any) -> Any:
        raw, _ = self._request(path, params=params or None)
        if not raw.strip():
            return {}
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            raise GitHubError("bad_output", "GitHub beklenen JSON biçiminde yanıt vermedi.") from None

    # --- cache -------------------------------------------------------------

    @staticmethod
    def _cache_get(key: str) -> Optional[tuple[float, str, bytes]]:
        with _CACHE_LOCK:
            entry = _CACHE.get(key)
            if entry is not None:
                _CACHE.move_to_end(key)
            return entry

    @staticmethod
    def _cache_put(key: str, etag: str, body: bytes) -> None:
        with _CACHE_LOCK:
            _CACHE[key] = (time.time(), etag, body)
            _CACHE.move_to_end(key)
            while len(_CACHE) > CACHE_MAX_ENTRIES:
                _CACHE.popitem(last=False)

    # --- paths -------------------------------------------------------------

    @staticmethod
    def _repo_path(owner: str, repo: str) -> str:
        owner = github_query.validate_owner(owner)
        repo = github_query.validate_repo(repo)
        return f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"

    @staticmethod
    def _quoted_path(path: str) -> str:
        path = github_query.validate_path(path)
        return "/".join(quote(segment, safe="") for segment in path.split("/")) if path else ""

    # --- reads -------------------------------------------------------------

    def rate_limit(self) -> dict[str, Any]:
        """Current limits. This endpoint itself is not charged."""
        data = self._json("/rate_limit")
        return data if isinstance(data, dict) else {}

    def search_repositories(
        self, q: str, *, sort: str = "best_match", limit: int = 10
    ) -> SearchPage:
        q = str(q or "").strip()[:256]
        if not q:
            raise GitHubError("bad_query", "boş arama")
        params: dict[str, Any] = {"q": q, "per_page": max(1, min(int(limit), MAX_SEARCH_RESULTS))}
        if sort in {"stars", "forks", "updated"}:
            params["sort"] = sort
            params["order"] = "desc"
        data = _as_dict(self._json("/search/repositories", **params))
        items = [i for i in (data.get("items") or []) if isinstance(i, dict)]
        total = data.get("total_count")
        return SearchPage(
            items=items[: params["per_page"]],
            total=total if isinstance(total, int) else len(items),
            incomplete=bool(data.get("incomplete_results")),
        )

    def get_repo(self, owner: str, repo: str) -> dict[str, Any]:
        return _as_dict(self._json(self._repo_path(owner, repo)))

    def get_readme(self, owner: str, repo: str, *, ref: str = "") -> tuple[str, bool]:
        """(README text, truncated). ("", False) when there is none."""
        params = {"ref": github_query.validate_ref(ref)} if ref else None
        try:
            raw, truncated = self._request(
                self._repo_path(owner, repo) + "/readme",
                params=params,
                accept=ACCEPT_RAW,
                max_bytes=MAX_README_BYTES,
                truncate=True,
            )
        except GitHubError as exc:
            if exc.code == "not_found":
                return "", False
            raise
        text, binary = _decode_text(raw)
        return ("" if binary else text), truncated

    def get_languages(self, owner: str, repo: str) -> dict[str, int]:
        data = self._json(self._repo_path(owner, repo) + "/languages")
        return {str(k): int(v) for k, v in data.items() if isinstance(v, int)} if isinstance(data, dict) else {}

    def get_latest_release(self, owner: str, repo: str) -> Optional[dict[str, Any]]:
        try:
            data = self._json(self._repo_path(owner, repo) + "/releases/latest")
        except GitHubError as exc:
            if exc.code == "not_found":
                return None
            raise
        return data if isinstance(data, dict) else None

    def list_directory(self, owner: str, repo: str, path: str = "", *, ref: str = "") -> TreeListing:
        """One directory level. Raises ``GitHubError('not_a_directory')`` for a file."""
        suffix = self._quoted_path(path)
        params = {"ref": github_query.validate_ref(ref)} if ref else {}
        data = self._json(self._repo_path(owner, repo) + "/contents" + (f"/{suffix}" if suffix else ""), **params)
        if not isinstance(data, list):
            raise GitHubError("not_a_directory", "bu yol bir dizin değil")
        entries = [
            (
                str(item.get("path") or item.get("name") or ""),
                "dir" if item.get("type") == "dir" else "file",
                int(item.get("size") or 0),
            )
            for item in data
            if isinstance(item, dict)
        ]
        entries.sort(key=lambda e: (e[1] != "dir", e[0].casefold()))
        truncated = len(entries) > MAX_TREE_ENTRIES
        return TreeListing(entries=entries[:MAX_TREE_ENTRIES], truncated=truncated)

    def get_file(self, owner: str, repo: str, path: str, *, ref: str = "") -> FileContent:
        """A file's text, cut at ``MAX_FILE_BYTES``.

        Asked for in the raw media type: one request for any size, where the
        JSON form stops including content above 1 MB. A directory answers
        the raw request with its JSON listing instead, which is how a
        ``blob`` link that is really a folder is told apart.
        """
        quoted = self._quoted_path(path)
        if not quoted:
            raise GitHubError("bad_request", "dosya yolu boş")
        params = {"ref": github_query.validate_ref(ref)} if ref else None
        base = self._repo_path(owner, repo)
        raw, truncated = self._request(
            f"{base}/contents/{quoted}",
            params=params,
            accept=ACCEPT_RAW,
            max_bytes=MAX_FILE_BYTES,
            truncate=True,
        )
        if raw.lstrip().startswith(b"[") and not truncated and _is_listing(raw):
            raise GitHubError("not_a_file", "bu yol bir dizin")
        text, binary = _decode_text(raw)
        web = self.config.web_url
        return FileContent(
            path=github_query.validate_path(path),
            size=len(raw),
            text=text,
            truncated=truncated,
            binary=binary,
            html_url=f"{web}/{owner}/{repo}/blob/{ref or 'HEAD'}/{github_query.validate_path(path)}",
        )

    def get_issue(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        """An issue or a pull request -- GitHub serves both from /issues."""
        number = github_query.validate_number(number)
        return _as_dict(self._json(f"{self._repo_path(owner, repo)}/issues/{number}"))

    def get_pull(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        number = github_query.validate_number(number)
        return _as_dict(self._json(f"{self._repo_path(owner, repo)}/pulls/{number}"))

    def recent_comments(
        self, owner: str, repo: str, number: int, *, total: int, limit: int = 8
    ) -> list[dict[str, Any]]:
        """The newest ``limit`` comments, oldest first. ``total`` comes from the
        issue itself, so the last page is fetched directly -- one request."""
        number = github_query.validate_number(number)
        limit = max(1, min(int(limit), 50))
        if total <= 0:
            return []
        page = max(1, (total + limit - 1) // limit)
        data = self._json(
            f"{self._repo_path(owner, repo)}/issues/{number}/comments",
            per_page=limit,
            page=page,
        )
        comments = [c for c in data if isinstance(c, dict)] if isinstance(data, list) else []
        if len(comments) < limit and page > 1:
            # The last page was short: the page before holds the rest of the
            # newest ``limit``.
            earlier = self._json(
                f"{self._repo_path(owner, repo)}/issues/{number}/comments",
                per_page=limit,
                page=page - 1,
            )
            if isinstance(earlier, list):
                comments = [c for c in earlier if isinstance(c, dict)] + comments
        return comments[-limit:]


def _as_dict(data: Any) -> dict[str, Any]:
    return data if isinstance(data, dict) else {}


def _is_listing(raw: bytes) -> bool:
    """A contents-API directory listing, as opposed to a file that happens
    to hold a JSON array: every entry carries GitHub's own keys."""
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return False
    return (
        isinstance(data, list)
        and bool(data)
        and all(isinstance(e, dict) and {"type", "sha", "path"} <= e.keys() for e in data)
    )


# --------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------

HEALTH_CACHE_SECONDS = 60
_HEALTH_CACHE: dict[str, tuple[float, "HealthReport"]] = {}


@dataclass(frozen=True)
class HealthReport:
    enabled: bool = False
    configured: bool = False
    reachable: bool = False
    authenticated: bool = False
    api_url: str = ""
    core_limit: Optional[int] = None
    core_remaining: Optional[int] = None
    search_limit: Optional[int] = None
    search_remaining: Optional[int] = None
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
            "authenticated": self.authenticated,
            "api_url": self.api_url,
            "core_limit": self.core_limit,
            "core_remaining": self.core_remaining,
            "search_limit": self.search_limit,
            "search_remaining": self.search_remaining,
            "detail": self.detail,
            "remedy": self.remedy,
            "error_code": self.error_code,
        }


def check_health(
    *, use_cache: bool = True, config: Optional[GitHubConfig] = None, session: Any = None
) -> HealthReport:
    """Can this deployment read GitHub right now? Never raises."""
    cfg = config or load_config()
    cache_key = f"{cfg.api_url}|{cfg.authenticated}|{cfg.enabled}"
    if use_cache:
        cached = _HEALTH_CACHE.get(cache_key)
        if cached and (time.time() - cached[0]) < HEALTH_CACHE_SECONDS:
            return cached[1]
    report = _probe(cfg, session)
    _HEALTH_CACHE[cache_key] = (time.time(), report)
    return report


def reset_health_cache() -> None:
    _HEALTH_CACHE.clear()


def _probe(cfg: GitHubConfig, session: Any) -> HealthReport:
    base = {"enabled": cfg.enabled, "api_url": cfg.api_url, "authenticated": cfg.authenticated}
    problems = cfg.problems()
    if problems:
        return HealthReport(
            **base, detail=" ".join(problems), remedy="docs/github.md", error_code="not_configured"
        )
    try:
        data = GitHubClient(cfg, session=session, use_cache=False).rate_limit()
    except GitHubError as exc:
        return HealthReport(
            **base, configured=True, detail=exc.detail, remedy=exc.remedy, error_code=exc.code
        )
    except Exception as exc:  # noqa: BLE001 - a broken probe is a report, not a crash
        return HealthReport(
            **base, configured=True,
            detail=f"GitHub durumu sorgulanamadı ({type(exc).__name__}).", error_code="failed",
        )
    resources = data.get("resources") if isinstance(data.get("resources"), dict) else {}
    core = resources.get("core") if isinstance(resources.get("core"), dict) else {}
    search = resources.get("search") if isinstance(resources.get("search"), dict) else {}
    mode = "token ile" if cfg.authenticated else "anonim"
    detail = (
        f"Bağlı ({mode}): saatlik {core.get('remaining', '?')}/{core.get('limit', '?')} istek, "
        f"dakikalık {search.get('remaining', '?')}/{search.get('limit', '?')} arama kaldı."
    )
    return HealthReport(
        **base,
        configured=True,
        reachable=True,
        core_limit=core.get("limit"),
        core_remaining=core.get("remaining"),
        search_limit=search.get("limit"),
        search_remaining=search.get("remaining"),
        detail=detail,
        remedy="" if cfg.authenticated else _REMEDY_TOKEN,
    )
