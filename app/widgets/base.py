"""
Shared scaffolding for bookmark live-stats widgets (Homepage-style).

A "provider" knows how to call one homelab service's API and reduce the response
to a small list of Stat cells. Providers register themselves with @register, which
records both the async fetch function and the UI field metadata used to build the
bookmark edit modal dynamically.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable

import httpx

# Generous-but-bounded timeout: homelab services on a LAN should answer fast; a
# slow/dead one must not hang the widget poll.
TIMEOUT = httpx.Timeout(6.0, connect=4.0)


@dataclass
class Stat:
    """One value/label cell rendered in a tile's stat strip."""
    label: str
    value: str

    def as_dict(self) -> dict:
        return {"label": self.label, "value": self.value}


@dataclass
class WidgetResult:
    ok: bool
    stats: list[Stat] = field(default_factory=list)
    error: str | None = None

    def as_dict(self) -> dict:
        return {"ok": self.ok,
                "stats": [s.as_dict() for s in self.stats],
                "error": self.error}

    @classmethod
    def of(cls, *stats: Stat) -> "WidgetResult":
        return cls(ok=True, stats=list(stats))

    @classmethod
    def fail(cls, error: str) -> "WidgetResult":
        return cls(ok=False, error=error)


# fetch signature: async (base_url, config, client) -> WidgetResult
FetchFn = Callable[[str, dict, httpx.AsyncClient], Awaitable[WidgetResult]]


@dataclass
class Provider:
    key: str
    label: str
    fetch: FetchFn
    fields: list[dict]          # UI inputs shown in the edit modal
    needs_url: bool = True      # whether the API-URL field is shown
    url_hint: str = ""          # placeholder for the API-URL field
    # Catalog of stats the user can pick from (Homepage-style "fields"). Each entry:
    # {"key", "label", optional "default": False to be off unless explicitly chosen}.
    # Empty for providers whose stats are dynamic (e.g. Home Assistant entities).
    stats: list[dict] = field(default_factory=list)

    def meta(self) -> dict:
        """JSON-safe metadata the front-end uses to render the config form."""
        return {"key": self.key, "label": self.label,
                "needs_url": self.needs_url, "url_hint": self.url_hint,
                "fields": self.fields, "stats": self.stats}


PROVIDERS: dict[str, Provider] = {}


def register(key: str, label: str, *, fields: list[dict] | None = None,
             needs_url: bool = True, url_hint: str = "", stats: list[dict] | None = None):
    """Decorator: register a provider's fetch fn + its UI field metadata."""
    def deco(fn: FetchFn) -> FetchFn:
        PROVIDERS[key] = Provider(key=key, label=label, fetch=fn,
                                  fields=fields or [], needs_url=needs_url,
                                  url_hint=url_hint, stats=stats or [])
        return fn
    return deco


def pick(config: dict, catalog: list[dict], available: dict[str, "Stat"]) -> list["Stat"]:
    """Reduce a provider's computed stats to the user's selection.

    `catalog` defines the full set + display order; `available` maps stat key ->
    Stat for the ones that could actually be computed this poll. The user's chosen
    keys live in config["stats"]. Order always follows the catalog (matches the
    checkbox order in the UI). When nothing is selected — a brand-new or pre-picker
    bookmark — fall back to the catalog's defaults (entries not marked default=False)."""
    chosen = set(config.get("stats") or [])
    if chosen:
        keys = [s["key"] for s in catalog
                if s["key"] in chosen and s["key"] in available]
    else:
        keys = []
    if not keys:  # no/blank/all-invalid selection → show catalog defaults
        keys = [s["key"] for s in catalog
                if s.get("default", True) and s["key"] in available]
    return [available[k] for k in keys]


# ── Small formatting / error helpers shared by providers ──

def compact(n) -> str:
    """Human-compact a count: 1234 -> '1.2k', 3_400_000 -> '3.4M'."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return str(n)
    for unit, div in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(n) >= div:
            return f"{n / div:.1f}".rstrip("0").rstrip(".") + unit
    return str(int(n))


def short_err(e: Exception) -> str:
    """A terse, user-facing reason for a failed fetch."""
    if isinstance(e, httpx.ConnectError):
        return "Can't reach service"
    if isinstance(e, httpx.TimeoutException):
        return "Timed out"
    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP {e.response.status_code}"
    if isinstance(e, httpx.HTTPError):
        return "Request failed"
    return type(e).__name__
