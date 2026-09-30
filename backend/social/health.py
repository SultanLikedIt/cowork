"""Is this platform actually usable right now, and if not, what fixes it.

Why this is not just "is the binary installed"
----------------------------------------------
Reddit has no anonymous path -- verified live, an unauthenticated search
returns ``forbidden`` -- so "installed" and "usable" are different questions,
and the gap between them is a session cookie that **expires in about seven
days**. In the container there is no browser, so rdt-cli's own refresh
("automatically attempts to refresh from the browser") can never run. That
makes cookie staleness a recurring operational fact of this feature rather
than an edge case, and something the user has to be told about *before* a
scheduled task starts failing at 3am.

Two design points worth stating:

* **This module never opens credential.json.** Staleness comes from the
  file's mtime, not its contents. The file holds a live account credential;
  a code path that reads it is a code path that can log it, and there is no
  reason to have one when ``os.stat`` answers the question.
* **``status`` is not a model-facing verb.** ``registry.build`` cannot
  produce it -- the model has no reason to ask -- so it is built here, via
  the same ``Command`` type and therefore the same scrubbed-environment
  runner as everything else.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from . import runner as runner_module
from .registry import PLATFORMS, Command, Platform

# rdt-cli's own documented cookie lifetime. Warn a day early: a task that
# runs on a schedule should not discover this at the moment it matters.
COOKIE_TTL_DAYS = 7
COOKIE_WARN_DAYS = 6

# Health costs a subprocess (~1s) and the answer does not change between two
# jobs starting a second apart. Short enough that a cookie refreshed by hand
# is picked up while the user is still watching.
CACHE_SECONDS = 60

_CACHE: dict[str, tuple[float, "HealthReport"]] = {}


@dataclass(frozen=True)
class HealthReport:
    platform: str
    installed: bool = False
    authenticated: bool = False
    username: str = ""
    cookie_age_days: Optional[float] = None
    detail: str = ""
    remedy: str = ""

    @property
    def ok(self) -> bool:
        return self.installed and self.authenticated

    @property
    def stale_soon(self) -> bool:
        """Working now, but close enough to expiry to be worth saying so."""
        return (
            self.ok
            and self.cookie_age_days is not None
            and self.cookie_age_days >= COOKIE_WARN_DAYS
        )

    def as_dict(self) -> dict[str, Any]:
        """Shape for /integrations/social and the UI badge."""
        return {
            "platform": self.platform,
            "ok": self.ok,
            "installed": self.installed,
            "authenticated": self.authenticated,
            "username": self.username,
            "cookie_age_days": (
                round(self.cookie_age_days, 1) if self.cookie_age_days is not None else None
            ),
            "stale_soon": self.stale_soon,
            "detail": self.detail,
            "remedy": self.remedy,
        }


def status_command(platform: Platform) -> Command:
    """The probe. Deliberately not reachable through ``registry.build``."""
    return Command(
        platform=platform.name,
        verb="status",
        argv=(platform.executable, "status", "--json"),
        timeout_seconds=20,
        credential_env=platform.credential_env,
    )


def _credential_path(platform: Platform) -> Path:
    """Where the tool keeps its session, under the child's redirected HOME."""
    if platform.name == "reddit":
        return runner_module.social_home() / ".config" / "rdt-cli" / "credential.json"
    return runner_module.social_home()


def _cookie_age_days(platform: Platform, reported_path: str = "") -> Optional[float]:
    """Age from mtime. Never reads the file -- see the module docstring.

    ``reported_path`` is what the tool itself said it is using, which is the
    authoritative answer when HOME redirection did not take effect (on
    Windows, ``Path.home()`` follows USERPROFILE, so the two can differ).
    """
    candidates = []
    if reported_path:
        candidates.append(Path(reported_path))
    candidates.append(_credential_path(platform))
    for path in candidates:
        try:
            stamp = path.stat().st_mtime
        except OSError:
            continue
        return max(0.0, (time.time() - stamp) / 86400.0)
    return None


_REMEDY_INSTALL = (
    "Kurulu değil. Backend imajına rdt-cli eklenmeli "
    "(docs/social-reach.md, sabitlenmiş sürüm)."
)
_REMEDY_COOKIE = (
    "Oturum çerezi yok veya süresi doldu. Cookie-Editor ile reddit.com'daki "
    "`reddit_session` değerini kopyalayıp credential.json içine yazın "
    "(docs/social-reach.md). Tarayıcıdan otomatik yenileme sunucuda mümkün "
    "değildir."
)


def check(
    platform_name: str = "reddit", *, use_cache: bool = True, run: Any = None
) -> HealthReport:
    """Probe one platform. Never raises; a broken tool is a report, not an error."""
    key = str(platform_name or "").strip().lower()
    platform = PLATFORMS.get(key)
    if platform is None:
        return HealthReport(platform=key, detail=f"Bilinmeyen platform: {key!r}")

    if use_cache:
        cached = _CACHE.get(key)
        if cached and (time.time() - cached[0]) < CACHE_SECONDS:
            return cached[1]

    report = _probe(platform, run or runner_module.run)
    _CACHE[key] = (time.time(), report)
    return report


def _probe(platform: Platform, run: Any) -> HealthReport:
    if platform.transport == "http":
        return _probe_http(platform, run)
    if not shutil.which(platform.executable):
        return HealthReport(
            platform=platform.name,
            installed=False,
            detail=f"'{platform.executable}' bulunamadı.",
            remedy=_REMEDY_INSTALL,
        )

    result = run(status_command(platform))
    if not result.ok:
        # The probe itself failing still tells us something useful: the tool
        # is installed but cannot report, which is a different remedy from
        # "not authenticated".
        return HealthReport(
            platform=platform.name,
            installed=True,
            detail=result.detail or "Durum sorgusu başarısız.",
            remedy=_REMEDY_COOKIE if result.needs_setup else platform.setup_hint,
        )

    data = result.data if isinstance(result.data, dict) else {}
    authenticated = bool(data.get("authenticated"))
    username = str(data.get("username") or "")
    age = _cookie_age_days(platform, str(data.get("credential_file") or ""))

    if not authenticated:
        return HealthReport(
            platform=platform.name,
            installed=True,
            authenticated=False,
            cookie_age_days=age,
            detail=str(data.get("error") or "Oturum açılmamış."),
            remedy=_REMEDY_COOKIE,
        )

    detail = f"Bağlı ({username})." if username else "Bağlı."
    remedy = ""
    if age is not None and age >= COOKIE_WARN_DAYS:
        detail += (
            f" Çerez {age:.0f} gündür yenilenmedi; ~{COOKIE_TTL_DAYS} günde "
            "bir yenilenmesi gerekir."
        )
        remedy = _REMEDY_COOKIE
    return HealthReport(
        platform=platform.name,
        installed=True,
        authenticated=True,
        username=username,
        cookie_age_days=age,
        detail=detail,
        remedy=remedy,
    )


def check_all(*, use_cache: bool = True) -> list[HealthReport]:
    return [check(name, use_cache=use_cache) for name in PLATFORMS]


def reset_cache() -> None:
    """For tests, and for an endpoint that wants a fresh answer on demand."""
    _CACHE.clear()


def _probe_http(platform: Platform, run: Any) -> HealthReport:
    """Reachability, for a platform with no credential to check.

    There is nothing to install and nothing to expire here, so the only
    question is whether the API answers -- which makes this report boring on
    purpose. It still does a real call rather than returning True: "the
    network is fine" is exactly the assumption that turns a dead upstream
    into a mysteriously empty answer.
    """
    from .registry import build

    try:
        probe = build(platform.name, "top", limit=1)
    except Exception:  # noqa: BLE001 - a broken builder is an unavailable
        return HealthReport(
            platform=platform.name,
            installed=True,
            detail="Sorgu üretilemedi.",
        )
    result = run(probe)
    if not result.ok and result.error_code == "network":
        # Observed right after `docker compose up`: the first probe failed
        # with ConnectionError while DNS was still settling, and a job
        # starting in that window would have been told the platform was
        # unavailable without ever trying. One retry, and only for a network
        # error -- an auth or rate-limit failure is a real answer and
        # retrying it just spends another request.
        result = run(probe)
    if not result.ok:
        return HealthReport(
            platform=platform.name,
            installed=True,
            authenticated=False,
            detail=result.detail or "Servise ulaşılamadı.",
            remedy=platform.setup_hint,
        )
    return HealthReport(
        platform=platform.name,
        installed=True,
        # No credential exists, so "authenticated" means "usable" here. The
        # alternative -- reporting False for a platform that works perfectly
        # -- would make `ok` useless as the UI's single boolean.
        authenticated=True,
        detail="Bağlı (herkese açık API, kimlik gerekmez).",
    )
