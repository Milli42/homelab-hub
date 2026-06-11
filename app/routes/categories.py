"""
Category CRUD routes. Handles both JSON and form-encoded requests.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, HTMLResponse
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Category, Task
from app.schemas import CategoryOut

router = APIRouter(prefix="/api/categories", tags=["categories"])


def _redirect(request: Request, url: str) -> RedirectResponse | HTMLResponse:
    if request.headers.get("HX-Request"):
        return HTMLResponse(status_code=200, headers={"HX-Redirect": url})
    return RedirectResponse(url=url, status_code=303)


async def _get_cat_or_404(db: AsyncSession, cat_id: int) -> Category:
    result = await db.execute(select(Category).where(Category.id == cat_id))
    cat = result.scalar_one_or_none()
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found")
    return cat


@router.get("", response_model=list[CategoryOut])
async def list_categories(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Category).order_by(Category.name))
    return result.scalars().all()


@router.post("", status_code=201)
async def create_category(request: Request, db: AsyncSession = Depends(get_db)):
    content_type = request.headers.get("content-type", "")

    if "application/json" in content_type:
        body = await request.json()
        name = body["name"]
        color = body.get("color", "#3b82f6")
        icon = body.get("icon", "🔧")
    else:
        form = await request.form()
        name = form.get("name")
        color = form.get("color", "#3b82f6")
        icon = form.get("icon", "🔧")

    if not name:
        raise HTTPException(status_code=422, detail="name is required")

    existing = await db.execute(select(Category).where(Category.name == name))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Category name already exists")

    cat = Category(name=name, color=color, icon=icon)
    db.add(cat)
    await db.commit()

    return _redirect(request, "/tasks#settings")


@router.put("/{cat_id}")
@router.post("/{cat_id}")
async def update_category(cat_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    cat = await _get_cat_or_404(db, cat_id)

    if request.method == "POST":
        form = await request.form()
        if form.get("_method", "").upper() != "PUT":
            raise HTTPException(status_code=405, detail="Use PUT or POST with _method=PUT")
        name = form.get("name")
        color = form.get("color")
        icon = form.get("icon")
    else:
        body = await request.json()
        name = body.get("name")
        color = body.get("color")
        icon = body.get("icon")

    if name:
        existing = await db.execute(
            select(Category).where(Category.name == name, Category.id != cat_id)
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Category name already exists")
        cat.name = name
    if color:
        cat.color = color
    if icon:
        cat.icon = icon

    await db.commit()
    return _redirect(request, "/tasks#settings")


@router.delete("/{cat_id}", status_code=204)
async def delete_category(cat_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    cat = await _get_cat_or_404(db, cat_id)
    # Null out category references on existing tasks
    await db.execute(update(Task).where(Task.category_id == cat_id).values(category_id=None))
    await db.delete(cat)
    await db.commit()

    if request.headers.get("HX-Request"):
        return HTMLResponse(
            headers={"HX-Redirect": "/tasks#settings"},
        )
    return RedirectResponse(url="/tasks#settings", status_code=303)
