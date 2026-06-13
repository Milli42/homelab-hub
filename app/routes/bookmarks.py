"""
Bookmarks module — a Homepage-style app launcher. Bookmarks are organized under
colored categories (BookmarkGroup) and rendered as sections of tiles. Each
bookmark has a title, URL, optional subtitle, and a smart icon (emoji or image
URL). Mirrors the Recipes module's category management and form-dispatch style.
"""
import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import Bookmark, BookmarkGroup

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

    return HTMLResponse(request.app.state.templates.get_template("bookmarks.html").render(
        request=request, groups=groups, total=total, groups_json=groups_json))


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

def _parse_bookmark_form(form) -> dict:
    gid = form.get("group_id")
    return {
        "group_id": int(gid) if gid else None,
        "title": str(form.get("title", "")).strip(),
        "url": str(form.get("url", "")).strip(),
        "subtitle": str(form.get("subtitle") or "").strip() or None,
        "icon": str(form.get("icon") or "").strip(),
        "width": _parse_width(form.get("width")),
    }


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
        data = _parse_bookmark_form(form)
        await _validate_bookmark(db, data)
        for k, v in data.items():
            setattr(bm, k, v)
        await db.commit()
        return _redirect(request, "/bookmarks")

    raise HTTPException(405, "Use _method=PUT or _method=DELETE")
