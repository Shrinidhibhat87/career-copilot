"""OpenRouter's model list and key check, the two free calls the Settings page needs.

Neither spends credit: `GET /models` is public, and `GET /key` reports a key's limit and
usage. Everything else about OpenRouter goes through litellm.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from career_copilot.domain.model_settings import LiveModel

API_BASE = "https://openrouter.ai/api/v1"
TIMEOUT = 10.0
CACHE_SECONDS = 3600.0
_PER_MILLION = 1_000_000


class OpenRouterUnavailable(RuntimeError):
    """OpenRouter could not be reached, or answered with something unreadable."""


@dataclass(frozen=True, slots=True)
class KeyCheck:
    valid: bool
    limit: float | None = None  # credit limit; None means no limit set on the key
    usage: float | None = None
    detail: str = ""


def _price(value: object) -> tuple[float | None, bool]:
    """Per-token price string -> (per million tokens, dynamic). OpenRouter lists -1 for a
    model priced only after the call."""
    try:
        number = float(str(value))
    except (TypeError, ValueError):
        return None, False
    if number < 0:
        return None, True
    return round(number * _PER_MILLION, 6), False


def parse_models(payload: object) -> dict[str, LiveModel]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise OpenRouterUnavailable("OpenRouter's model list did not parse")
    models: dict[str, LiveModel] = {}
    for row in payload["data"]:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        pricing = row.get("pricing") or {}
        price_in, dynamic_in = _price(pricing.get("prompt"))
        price_out, dynamic_out = _price(pricing.get("completion"))
        model_id = str(row["id"])
        models[model_id] = LiveModel(
            id=model_id,
            name=str(row.get("name") or model_id),
            caps=frozenset(str(p) for p in row.get("supported_parameters") or ()),
            price_in=price_in,
            price_out=price_out,
            dynamic=dynamic_in or dynamic_out,
        )
    return models


class OpenRouterDirectory:
    """The live model list, cached in memory for an hour, and the key check."""

    def __init__(
        self,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        base_url: str = API_BASE,
    ) -> None:
        self._transport = transport
        self._clock = clock
        self._base = base_url.rstrip("/")
        self._lock = threading.Lock()
        self._cached: tuple[float, dict[str, LiveModel]] | None = None

    def _client(self) -> httpx.Client:
        return httpx.Client(transport=self._transport, timeout=TIMEOUT)

    def models(self) -> dict[str, LiveModel]:
        with self._lock:
            if self._cached and self._clock() - self._cached[0] < CACHE_SECONDS:
                return self._cached[1]
        try:
            with self._client() as http:
                response = http.get(f"{self._base}/models")
                response.raise_for_status()
                models = parse_models(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise OpenRouterUnavailable(
                f"could not read OpenRouter's model list ({type(exc).__name__})"
            ) from None
        with self._lock:
            self._cached = (self._clock(), models)
        return models

    def check_key(self, key: str) -> KeyCheck:
        """Raises `OpenRouterUnavailable` when the answer says nothing about the key."""
        try:
            with self._client() as http:
                response = http.get(f"{self._base}/key", headers={"Authorization": f"Bearer {key}"})
        except httpx.HTTPError as exc:
            raise OpenRouterUnavailable(
                f"could not reach OpenRouter ({type(exc).__name__})"
            ) from None
        if response.status_code in (401, 403):
            return KeyCheck(valid=False, detail="OpenRouter rejected this key")
        if response.status_code != 200:
            raise OpenRouterUnavailable(f"OpenRouter answered {response.status_code}")
        try:
            data = response.json().get("data") or {}
        except ValueError:
            raise OpenRouterUnavailable("OpenRouter's key check did not parse") from None
        limit, usage = data.get("limit"), data.get("usage")
        return KeyCheck(
            valid=True,
            limit=float(limit) if isinstance(limit, int | float) else None,
            usage=float(usage) if isinstance(usage, int | float) else None,
        )
