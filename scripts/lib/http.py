"""Client HTTP minimal (urllib) avec limitation de débit et reprise sur erreur.

L'API Recherche d'entreprises plafonne à 7 requêtes/seconde par IP et 30/seconde par ASN,
et répond 429 avec un en-tête `Retry-After` au-delà. Les runners GitHub Actions étant sur
cloud public, on reste volontairement sous la limite et on respecte `Retry-After`.
"""

from __future__ import annotations

import gzip
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class HttpError(RuntimeError):
    """Erreur HTTP non récupérable, ou récupérable mais épuisée en tentatives."""

    def __init__(self, message: str, status: int | None = None, url: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.url = url


class RateLimiter:
    """Espacement minimal entre deux requêtes (débit moyen garanti)."""

    def __init__(self, per_second: float) -> None:
        self.min_interval = 1.0 / per_second if per_second > 0 else 0.0
        self._next_allowed = 0.0

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        now = time.monotonic()
        if now < self._next_allowed:
            time.sleep(self._next_allowed - now)
            now = time.monotonic()
        self._next_allowed = now + self.min_interval


class HttpClient:
    """Client GET avec `User-Agent` explicite, limitation de débit et backoff."""

    def __init__(
        self,
        user_agent: str,
        per_second: float = 0.0,
        timeout: float = 60.0,
        max_attempts: int = 5,
        base_backoff: float = 2.0,
    ) -> None:
        self.user_agent = user_agent
        self.limiter = RateLimiter(per_second)
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.base_backoff = base_backoff

    # -- interne ---------------------------------------------------------------

    @staticmethod
    def _build_url(url: str, params: dict[str, Any] | None) -> str:
        if not params:
            return url
        cleaned = {k: v for k, v in params.items() if v is not None and v != ""}
        if not cleaned:
            return url
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}{urllib.parse.urlencode(cleaned)}"

    @staticmethod
    def _decode_body(response: Any) -> bytes:
        raw = response.read()
        if response.headers.get("Content-Encoding", "").lower() == "gzip":
            return gzip.decompress(raw)
        return raw

    @staticmethod
    def _retry_after(headers: Any, fallback: float) -> float:
        value = headers.get("Retry-After") if headers else None
        if not value:
            return fallback
        try:
            return max(float(value), 0.0)
        except (TypeError, ValueError):
            # Forme date HTTP : on ne cherche pas à la parser, le repli suffit.
            return fallback

    # -- API publique ----------------------------------------------------------

    def get_bytes(self, url: str, params: dict[str, Any] | None = None) -> bytes:
        full_url = self._build_url(url, params)
        request = urllib.request.Request(
            full_url,
            headers={"User-Agent": self.user_agent, "Accept-Encoding": "gzip"},
            method="GET",
        )

        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            self.limiter.wait()
            backoff = self.base_backoff * (2 ** (attempt - 1))
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return self._decode_body(response)
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code not in RETRYABLE_STATUS or attempt == self.max_attempts:
                    raise HttpError(
                        f"HTTP {exc.code} sur {full_url}", status=exc.code, url=full_url
                    ) from exc
                delay = self._retry_after(exc.headers, backoff)
                log.warning(
                    "HTTP %s sur %s — nouvelle tentative dans %.1f s (%s/%s)",
                    exc.code,
                    full_url,
                    delay,
                    attempt,
                    self.max_attempts,
                )
                time.sleep(delay)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt == self.max_attempts:
                    raise HttpError(f"Échec réseau sur {full_url} : {exc}", url=full_url) from exc
                log.warning(
                    "Échec réseau sur %s (%s) — nouvelle tentative dans %.1f s (%s/%s)",
                    full_url,
                    exc,
                    backoff,
                    attempt,
                    self.max_attempts,
                )
                time.sleep(backoff)

        raise HttpError(f"Échec sur {full_url} : {last_error}", url=full_url)

    def get_text(self, url: str, params: dict[str, Any] | None = None) -> str:
        return self.get_bytes(url, params).decode("utf-8", errors="replace")

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        body = self.get_bytes(url, params)
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            preview = body[:200].decode("utf-8", errors="replace")
            raise HttpError(f"Réponse non JSON depuis {url} : {preview!r}", url=url) from exc
