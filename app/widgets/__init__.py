"""
Bookmark live-stats widgets (Homepage-style).

Public surface:
  PROVIDERS            — registry of available providers (key -> Provider)
  provider_meta()      — JSON-safe list of provider UI metadata for the edit modal
  fetch_widget(bm)     — call the bookmark's provider, return a WidgetResult
  aclose()             — close the shared httpx clients (lifespan shutdown)
"""
from __future__ import annotations

import httpx

from .base import PROVIDERS, TIMEOUT, WidgetResult, short_err
from . import providers as _providers  # noqa: F401  (import registers the providers)

# Two long-lived clients keyed by TLS-verify mode — httpx fixes `verify` at client
# construction, so we keep one verifying and one non-verifying client and pool both.
_clients: dict[bool, httpx.AsyncClient] = {}


def _client(verify: bool) -> httpx.AsyncClient:
    cli = _clients.get(verify)
    if cli is None or cli.is_closed:
        cli = httpx.AsyncClient(verify=verify, timeout=TIMEOUT, follow_redirects=True)
        _clients[verify] = cli
    return cli


def provider_meta() -> list[dict]:
    """UI metadata for every provider, ordered by label — feeds the edit modal."""
    return [p.meta() for p in sorted(PROVIDERS.values(), key=lambda p: p.label)]


async def fetch_widget(bookmark) -> WidgetResult:
    """Dispatch to the bookmark's provider. Never raises — failures come back as
    WidgetResult.fail so the tile can show a quiet offline state."""
    provider = PROVIDERS.get((bookmark.widget_type or "").strip())
    if not provider:
        return WidgetResult.fail("Unknown widget type")
    base_url = (bookmark.widget_api_url or "").rstrip("/")
    if not base_url:
        return WidgetResult.fail("No URL configured")
    config = bookmark.widget_config_dict
    verify = bool(config.get("verify_tls", True))  # "Ignore cert" toggle stores False
    try:
        return await provider.fetch(base_url, config, _client(verify))
    except Exception as e:  # noqa: BLE001 — any provider error becomes a quiet failure
        return WidgetResult.fail(short_err(e))


async def aclose() -> None:
    for cli in _clients.values():
        await cli.aclose()
    _clients.clear()
