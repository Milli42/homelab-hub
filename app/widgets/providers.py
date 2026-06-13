"""
The bundled widget providers. Each fetch fn receives a normalized base URL, the
bookmark's widget config dict, and a shared httpx.AsyncClient (its TLS-verify mode
is already chosen by the dispatcher). It returns a WidgetResult of a few Stat cells.

Endpoint/field shapes follow gethomepage.dev's integrations; per-service quirks are
expected to be tuned against live services during testing.
"""
from __future__ import annotations

from .base import Stat, WidgetResult, compact, register

# Credential field reused by api-key services.
_API_KEY = [{"name": "api_key", "label": "API Key", "type": "password", "required": True}]


# ── Home Assistant ────────────────────────────────────────
@register("homeassistant", "Home Assistant", url_hint="http://homeassistant.local:8123",
          fields=[
              {"name": "token", "label": "Long-Lived Access Token", "type": "password", "required": True},
              {"name": "entities", "label": "Entities to show", "type": "entities", "required": True},
          ])
async def home_assistant(base_url, config, client) -> WidgetResult:
    token = (config.get("token") or "").strip()
    entities = config.get("entities") or []
    if not token or not entities:
        return WidgetResult.fail("Token and at least one entity required")
    r = await client.get(f"{base_url}/api/states",
                         headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    states = {s.get("entity_id"): s for s in r.json()}
    stats = []
    for ent in entities[:6]:
        eid = (ent.get("entity_id") or "").strip()
        if not eid:
            continue
        s = states.get(eid)
        label = (ent.get("label") or "").strip()
        if not s:
            stats.append(Stat(label or eid, "—"))
            continue
        attrs = s.get("attributes") or {}
        label = label or attrs.get("friendly_name") or eid
        unit = (ent.get("unit") or attrs.get("unit_of_measurement") or "").strip()
        value = f"{s.get('state', '—')} {unit}".strip()
        stats.append(Stat(label, value))
    return WidgetResult(ok=True, stats=stats)


# ── Uptime Kuma (public status page) ──────────────────────
@register("uptimekuma", "Uptime Kuma", url_hint="https://status.example.com",
          fields=[{"name": "slug", "label": "Status Page Slug", "type": "text", "required": True}])
async def uptime_kuma(base_url, config, client) -> WidgetResult:
    slug = (config.get("slug") or "").strip()
    if not slug:
        return WidgetResult.fail("Status page slug required")
    r = await client.get(f"{base_url}/api/status-page/heartbeat/{slug}")
    r.raise_for_status()
    data = r.json()
    heartbeats = data.get("heartbeatList") or {}
    uptime = data.get("uptimeList") or {}
    up = down = 0
    for beats in heartbeats.values():
        if not beats:
            continue
        if beats[-1].get("status") == 1:
            up += 1
        else:
            down += 1
    day_vals = [v for k, v in uptime.items() if str(k).endswith("_24")]
    stats = [Stat("Up", str(up)), Stat("Down", str(down))]
    if day_vals:
        stats.append(Stat("Uptime", f"{round(sum(day_vals) / len(day_vals) * 100, 1)}%"))
    return WidgetResult(ok=True, stats=stats)


# ── AdGuard Home ──────────────────────────────────────────
@register("adguard", "AdGuard Home", url_hint="http://adguard.local:3000",
          fields=[
              {"name": "username", "label": "Username", "type": "text", "required": True},
              {"name": "password", "label": "Password", "type": "password", "required": True},
          ])
async def adguard(base_url, config, client) -> WidgetResult:
    auth = (config.get("username", ""), config.get("password", ""))
    r = await client.get(f"{base_url}/control/stats", auth=auth)
    r.raise_for_status()
    d = r.json()
    queries = d.get("num_dns_queries", 0) or 0
    blocked = d.get("num_blocked_filtering", 0) or 0
    pct = round(blocked / queries * 100, 1) if queries else 0
    return WidgetResult.of(
        Stat("Queries", compact(queries)),
        Stat("Blocked", compact(blocked)),
        Stat("Blocked", f"{pct}%"),
    )


# ── Nginx Proxy Manager ───────────────────────────────────
@register("npm", "Nginx Proxy Manager", url_hint="http://npm.local:81",
          fields=[
              {"name": "username", "label": "Email", "type": "text", "required": True},
              {"name": "password", "label": "Password", "type": "password", "required": True},
          ])
async def nginx_proxy_manager(base_url, config, client) -> WidgetResult:
    # NPM uses short-lived bearer tokens; with the widget cache, one login per poll
    # is acceptable, so we don't bother persisting the token.
    tok = await client.post(f"{base_url}/api/tokens",
                            json={"identity": config.get("username", ""),
                                  "secret": config.get("password", "")})
    tok.raise_for_status()
    token = tok.json().get("token")
    if not token:
        return WidgetResult.fail("Login failed")
    r = await client.get(f"{base_url}/api/nginx/proxy-hosts",
                         headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    hosts = r.json()
    enabled = sum(1 for h in hosts if h.get("enabled"))
    total = len(hosts)
    return WidgetResult.of(
        Stat("Enabled", str(enabled)),
        Stat("Disabled", str(total - enabled)),
        Stat("Total", str(total)),
    )


# ── SABnzbd ───────────────────────────────────────────────
@register("sabnzbd", "SABnzbd", url_hint="http://sabnzbd.local:8080", fields=_API_KEY)
async def sabnzbd(base_url, config, client) -> WidgetResult:
    r = await client.get(f"{base_url}/api",
                         params={"mode": "queue", "output": "json",
                                 "apikey": config.get("api_key", "")})
    r.raise_for_status()
    body = r.json()
    if "queue" not in body:
        return WidgetResult.fail(str(body.get("error") or "Bad API key"))
    q = body["queue"]
    speed = (q.get("speed") or "0").strip()           # e.g. "1.2 M" (MB/s)
    return WidgetResult.of(
        Stat("Rate", f"{speed}B/s"),                  # -> "1.2 MB/s"
        Stat("Queue", str(q.get("noofslots", 0))),
        Stat("Left", q.get("timeleft", "0:00:00")),
    )


# ── Jellyfin (active streams) ─────────────────────────────
@register("jellyfin", "Jellyfin", url_hint="http://jellyfin.local:8096", fields=_API_KEY)
async def jellyfin(base_url, config, client) -> WidgetResult:
    r = await client.get(f"{base_url}/Sessions",
                         headers={"X-Emby-Token": config.get("api_key", "")})
    r.raise_for_status()
    playing = [s for s in r.json() if s.get("NowPlayingItem")]
    transcodes = sum(1 for s in playing if s.get("TranscodingInfo"))
    return WidgetResult.of(
        Stat("Streams", str(len(playing))),
        Stat("Direct", str(len(playing) - transcodes)),
        Stat("Transcode", str(transcodes)),
    )


# ── Radarr ────────────────────────────────────────────────
@register("radarr", "Radarr", url_hint="http://radarr.local:7878", fields=_API_KEY)
async def radarr(base_url, config, client) -> WidgetResult:
    h = {"X-Api-Key": config.get("api_key", "")}
    movies = await client.get(f"{base_url}/api/v3/movie", headers=h)
    movies.raise_for_status()
    mlist = movies.json()
    missing = sum(1 for m in mlist if m.get("monitored") and not m.get("hasFile"))
    queue = await client.get(f"{base_url}/api/v3/queue", headers=h, params={"pageSize": 1})
    queue.raise_for_status()
    return WidgetResult.of(
        Stat("Movies", str(len(mlist))),
        Stat("Missing", str(missing)),
        Stat("Queue", str(queue.json().get("totalRecords", 0))),
    )


# ── Sonarr ────────────────────────────────────────────────
@register("sonarr", "Sonarr", url_hint="http://sonarr.local:8989", fields=_API_KEY)
async def sonarr(base_url, config, client) -> WidgetResult:
    h = {"X-Api-Key": config.get("api_key", "")}
    series = await client.get(f"{base_url}/api/v3/series", headers=h)
    series.raise_for_status()
    wanted = await client.get(f"{base_url}/api/v3/wanted/missing", headers=h, params={"pageSize": 1})
    wanted.raise_for_status()
    queue = await client.get(f"{base_url}/api/v3/queue", headers=h, params={"pageSize": 1})
    queue.raise_for_status()
    return WidgetResult.of(
        Stat("Series", str(len(series.json()))),
        Stat("Wanted", str(wanted.json().get("totalRecords", 0))),
        Stat("Queue", str(queue.json().get("totalRecords", 0))),
    )


# ── Prowlarr ──────────────────────────────────────────────
@register("prowlarr", "Prowlarr", url_hint="http://prowlarr.local:9696", fields=_API_KEY)
async def prowlarr(base_url, config, client) -> WidgetResult:
    h = {"X-Api-Key": config.get("api_key", "")}
    idx = await client.get(f"{base_url}/api/v1/indexer", headers=h)
    idx.raise_for_status()
    enabled = sum(1 for i in idx.json() if i.get("enable"))
    stats = await client.get(f"{base_url}/api/v1/indexerstats", headers=h)
    stats.raise_for_status()
    rows = stats.json().get("indexers") or []
    grabs = sum(i.get("numberOfGrabs", 0) for i in rows)
    queries = sum(i.get("numberOfQueries", 0) for i in rows)
    return WidgetResult.of(
        Stat("Indexers", str(enabled)),
        Stat("Grabs", compact(grabs)),
        Stat("Queries", compact(queries)),
    )
