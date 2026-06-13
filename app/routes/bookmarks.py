"""
Bookmarks module — a Homepage-style app launcher. Bookmarks are organized under
colored categories (BookmarkGroup) and rendered as sections of tiles. Each
bookmark has a title, URL, optional subtitle, and a smart icon (emoji or image
URL). Mirrors the Recipes module's category management and form-dispatch style.
"""
import json
import time
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import Bookmark, BookmarkGroup
from app.widgets import PROVIDERS, fetch_widget, provider_meta

router = APIRouter(tags=["bookmarks"])


def _redirect(request: Request, url: str):
    if request.headers.get("HX-Request"):
        return HTMLResponse(status_code=200, headers={"HX-Redirect": url})
    return RedirectResponse(url=url, status_code=303)


_ALLOWED_WIDTHS = {2, 3, 4, 6, 8, 12}   # span of a 12-col grid: ⅙ ¼ ⅓ ½ ⅔ full


def _parse_width(value) -> int:
    """A bookmark's tile width as a span of a 12-column grid; defaults to ⅓ (4)."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 4
    return n if n in _ALLOWED_WIDTHS else 4


# ── Page ──────────────────────────────────────────────────

@router.get("/bookmarks", response_class=HTMLResponse)
async def bookmarks_page(request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(BookmarkGroup).options(selectinload(BookmarkGroup.bookmarks))
        .order_by(BookmarkGroup.sort_order, BookmarkGroup.name))
    groups = result.scalars().all()
    total = sum(len(g.bookmarks) for g in groups)

    # Lightweight group list for the JS-built bookmark modal's category picker.
    groups_json = json.dumps(
        [{"id": g.id, "name": g.name, "icon": g.icon} for g in groups]
    ).replace("</", "<\\/")  # harden against </script> breakout; rendered with |safe

    # Provider metadata drives the modal's "Live widget" config form.
    providers_json = json.dumps(provider_meta()).replace("</", "<\\/")

    return HTMLResponse(request.app.state.templates.get_template("bookmarks.html").render(
        request=request, groups=groups, total=total,
        groups_json=groups_json, providers_json=providers_json))


# ── Bookmark groups (categories) ──────────────────────────

@router.post("/api/bookmark-groups")
async def create_bookmark_group(request: Request, db: AsyncSession = Depends(get_db)):
    form = await request.form()
    name = str(form.get("name", "")).strip()
    if not name:
        raise HTTPException(422, "name required")
    next_order = (await db.scalar(select(func.max(BookmarkGroup.sort_order))) or 0) + 1
    db.add(BookmarkGroup(name=name,
                         color=str(form.get("color") or "#60a5fa"),
                         icon=str(form.get("icon") or "🔖"),
                         sort_order=next_order))
    await db.commit()
    return _redirect(request, "/bookmarks#bookmarks-settings")


# Declared before /{group_id} so "reorder" isn't captured as a group id.
@router.post("/api/bookmark-groups/reorder")
async def reorder_bookmark_groups(request: Request, db: AsyncSession = Depends(get_db)):
    """Persist category (section) order. Body: {"groups": [id, id, ...]}."""
    body = await request.json()
    for idx, gid in enumerate(body.get("groups", [])):
        grp = await db.get(BookmarkGroup, gid)
        if grp:
            grp.sort_order = idx
    await db.commit()
    return {"ok": True}


@router.post("/api/bookmark-groups/{group_id}")
async def bookmark_group_dispatch(group_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    form = await request.form()
    method = str(form.get("_method", "")).upper()
    group = await db.get(BookmarkGroup, group_id)
    if not group:
        raise HTTPException(404, "Group not found")

    if method == "DELETE":
        await db.delete(group)   # cascade removes its bookmarks
        await db.commit()
        return _redirect(request, "/bookmarks#bookmarks-settings")

    if method == "PUT":
        name = str(form.get("name") or "").strip()
        if name:
            group.name = name
        if form.get("color"):
            group.color = str(form.get("color"))
        if form.get("icon"):
            group.icon = str(form.get("icon"))
        await db.commit()
        return _redirect(request, "/bookmarks#bookmarks-settings")

    raise HTTPException(405, "Use _method=PUT or _method=DELETE")


# ── Bookmarks ─────────────────────────────────────────────

def _build_widget_config(form, widget_type: str, existing: dict) -> dict:
    """Assemble widget_config from the provider's declared fields. Blank password
    fields on edit preserve the previously stored secret (merge with `existing`)."""
    provider = PROVIDERS[widget_type]
    cfg: dict = {}
    for f in provider.fields:
        name, ftype = f["name"], f.get("type", "text")
        if ftype == "entities":
            ids = form.getlist("w_entity_id")
            labels = form.getlist("w_entity_label")
            units = form.getlist("w_entity_unit")
            ents = []
            for i, eid in enumerate(ids):
                eid = str(eid or "").strip()
                if not eid:
                    continue
                ents.append({
                    "entity_id": eid,
                    "label": str(labels[i] if i < len(labels) else "").strip(),
                    "unit": str(units[i] if i < len(units) else "").strip(),
                })
            cfg[name] = ents
        elif ftype == "password":
            val = str(form.get(f"w_{name}") or "")
            cfg[name] = val if val else str(existing.get(name, ""))  # blank → keep current
        else:
            cfg[name] = str(form.get(f"w_{name}") or "").strip()
    # Stat picker (Homepage-style): which stats the user ticked. Stored only when
    # the provider offers a catalog and at least one box is checked; an empty
    # selection falls back to the provider's defaults at render time.
    if provider.stats:
        chosen = [s.strip() for s in form.getlist("w_stats") if s.strip()]
        if chosen:
            cfg["stats"] = chosen
    # Every provider makes HTTP calls, so the TLS toggle is global to the form.
    cfg["verify_tls"] = not bool(form.get("w_ignore_tls"))
    return cfg


def _parse_bookmark_form(form, existing_config: dict | None = None) -> dict:
    gid = form.get("group_id")
    widget_type = str(form.get("widget_type") or "").strip()
    valid_widget = widget_type if widget_type in PROVIDERS else ""
    data = {
        "group_id": int(gid) if gid else None,
        "title": str(form.get("title", "")).strip(),
        "url": str(form.get("url", "")).strip(),
        "subtitle": str(form.get("subtitle") or "").strip() or None,
        "icon": str(form.get("icon") or "").strip(),
        "width": _parse_width(form.get("width")),
        "widget_type": valid_widget or None,
        "widget_url": (str(form.get("widget_url") or "").strip() or None) if valid_widget else None,
        "widget_config": None,
    }
    if valid_widget:
        cfg = _build_widget_config(form, valid_widget, existing_config or {})
        data["widget_config"] = json.dumps(cfg) if cfg else None
    return data


async def _validate_bookmark(db: AsyncSession, data: dict):
    if not data["title"] or not data["url"]:
        raise HTTPException(422, "title and url required")
    if not data["group_id"] or not await db.get(BookmarkGroup, data["group_id"]):
        raise HTTPException(422, "valid category required")


@router.post("/api/bookmarks")
async def create_bookmark(request: Request, db: AsyncSession = Depends(get_db)):
    data = _parse_bookmark_form(await request.form())
    await _validate_bookmark(db, data)
    next_order = (await db.scalar(
        select(func.max(Bookmark.sort_order)).where(Bookmark.group_id == data["group_id"])) or 0) + 1
    db.add(Bookmark(**data, sort_order=next_order))
    await db.commit()
    return _redirect(request, "/bookmarks")


# Declared before /{bookmark_id} so "reorder" isn't captured as a bookmark id.
@router.post("/api/bookmarks/reorder")
async def reorder_bookmarks(request: Request, db: AsyncSession = Depends(get_db)):
    """Persist the full layout. Body: {"groups": [{"group_id", "bookmarks": [id,...]}]}.
    Handles both within-category reordering and moves between categories."""
    body = await request.json()
    valid_groups = {g for (g,) in (await db.execute(select(BookmarkGroup.id))).all()}
    for grp in body.get("groups", []):
        gid = grp.get("group_id")
        if gid not in valid_groups:
            continue
        for idx, bid in enumerate(grp.get("bookmarks", [])):
            bm = await db.get(Bookmark, bid)
            if bm:
                bm.group_id = gid
                bm.sort_order = idx
    await db.commit()
    return {"ok": True}


@router.post("/api/bookmarks/{bookmark_id}")
async def bookmark_dispatch(bookmark_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    form = await request.form()
    method = str(form.get("_method", "")).upper()
    bm = await db.get(Bookmark, bookmark_id)
    if not bm:
        raise HTTPException(404, "Bookmark not found")

    if method == "DELETE":
        await db.delete(bm)
        await db.commit()
        return _redirect(request, "/bookmarks")

    if method == "PUT":
        data = _parse_bookmark_form(form, bm.widget_config_dict)
        await _validate_bookmark(db, data)
        for k, v in data.items():
            setattr(bm, k, v)
        await db.commit()
        return _redirect(request, "/bookmarks")

    raise HTTPException(405, "Use _method=PUT or _method=DELETE")


# ── Widget live-stats proxy ───────────────────────────────

# Short in-process cache so many open tabs / fast polls don't hammer the service.
_WIDGET_CACHE: dict[int, tuple[float, dict]] = {}
_WIDGET_TTL = 25.0


@router.get("/api/bookmarks/{bookmark_id}/widget")
async def bookmark_widget(bookmark_id: int, db: AsyncSession = Depends(get_db)):
    """Server-side fetch of a bookmark's provider stats. Returns
    {ok, stats: [{label, value}], error}; never exposes credentials."""
    now = time.monotonic()
    cached = _WIDGET_CACHE.get(bookmark_id)
    if cached and now - cached[0] < _WIDGET_TTL:
        return cached[1]
    bm = await db.get(Bookmark, bookmark_id)
    if not bm:
        raise HTTPException(404, "Bookmark not found")
    if not bm.has_widget:
        return {"ok": False, "stats": [], "error": "No widget configured"}
    result = (await fetch_widget(bm)).as_dict()
    _WIDGET_CACHE[bookmark_id] = (now, result)
    return result
