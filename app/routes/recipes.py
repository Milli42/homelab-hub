"""
Recipes module — a simple recipe box. Each recipe has one category, optional
prep/cook times, a line-per-item ingredient list, ordered directions, and photo
attachments with server-generated thumbnails. Mirrors the Notes module's image
handling (Pillow WebP thumbnails) and the Tasks module's category management.
"""
import os
import shutil
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import Recipe, RecipeCategory, RecipeImage

router = APIRouter(tags=["recipes"])

RECIPES_DIR = os.environ.get("RECIPES_DIR", "data/recipes")
THUMBS_DIR = os.path.join(RECIPES_DIR, "thumbs")
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
THUMB_SIZE = 600

os.makedirs(RECIPES_DIR, exist_ok=True)
os.makedirs(THUMBS_DIR, exist_ok=True)


def _redirect(request: Request, url: str):
    if request.headers.get("HX-Request"):
        return HTMLResponse(status_code=200, headers={"HX-Redirect": url})
    return RedirectResponse(url=url, status_code=303)


def _now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def _unlink(path: str):
    try:
        os.remove(path)
    except OSError:
        pass


def _make_thumbnail(src_path: str, thumb_path: str):
    """Generate a webp thumbnail capped at THUMB_SIZE px on the long edge."""
    from PIL import Image, ImageOps
    with Image.open(src_path) as im:
        im = ImageOps.exif_transpose(im)
        im.thumbnail((THUMB_SIZE, THUMB_SIZE))
        if im.mode in ("RGBA", "P"):
            im = im.convert("RGB")
        im.save(thumb_path, "WEBP", quality=82)


# ── Pages ─────────────────────────────────────────────────

@router.get("/recipes", response_class=HTMLResponse)
async def recipes_page(request: Request, category_id: str | None = Query(None),
                       db: AsyncSession = Depends(get_db)):
    cat_filter: int | None = None
    if category_id and category_id.strip():
        try:
            cat_filter = int(category_id)
        except ValueError:
            pass

    q = (select(Recipe).options(selectinload(Recipe.category), selectinload(Recipe.images))
         .order_by(Recipe.title))
    if cat_filter is not None:
        q = q.where(Recipe.category_id == cat_filter)
    recipes = (await db.execute(q)).scalars().all()
    categories = (await db.execute(select(RecipeCategory).order_by(RecipeCategory.name))).scalars().all()
    total = await db.scalar(select(Recipe.id).limit(1))

    return HTMLResponse(request.app.state.templates.get_template("recipes.html").render(
        request=request, recipes=recipes, categories=categories,
        selected_category=cat_filter, has_any=total is not None))


@router.get("/recipes/new", response_class=HTMLResponse)
async def recipe_new_form(request: Request, db: AsyncSession = Depends(get_db)):
    categories = (await db.execute(select(RecipeCategory).order_by(RecipeCategory.name))).scalars().all()
    return HTMLResponse(request.app.state.templates.get_template("recipe_form.html").render(
        request=request, recipe=None, categories=categories))


@router.get("/recipes/{recipe_id}", response_class=HTMLResponse)
async def recipe_detail(request: Request, recipe_id: int, edit: int = 0,
                        db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Recipe).options(selectinload(Recipe.category), selectinload(Recipe.images))
        .where(Recipe.id == recipe_id))
    recipe = result.scalar_one_or_none()
    if not recipe:
        return HTMLResponse("<div class='p-8 text-center text-red-400'>Recipe not found</div>", status_code=404)
    categories = (await db.execute(select(RecipeCategory).order_by(RecipeCategory.name))).scalars().all()
    if edit:
        return HTMLResponse(request.app.state.templates.get_template("recipe_form.html").render(
            request=request, recipe=recipe, categories=categories))
    return HTMLResponse(request.app.state.templates.get_template("recipe_detail.html").render(
        request=request, recipe=recipe))


# ── Recipes API ───────────────────────────────────────────

def _parse_recipe_form(form) -> dict:
    cat = form.get("category_id")
    return {
        "title": str(form.get("title", "")).strip(),
        "category_id": int(cat) if cat else None,
        "prep_time": str(form.get("prep_time") or "").strip() or None,
        "cook_time": str(form.get("cook_time") or "").strip() or None,
        "ingredients": str(form.get("ingredients") or ""),
        "directions": str(form.get("directions") or ""),
    }


@router.post("/api/recipes")
async def create_recipe(request: Request, db: AsyncSession = Depends(get_db)):
    data = _parse_recipe_form(await request.form())
    if not data["title"]:
        raise HTTPException(422, "title required")
    recipe = Recipe(**data)
    db.add(recipe)
    await db.commit()
    await db.refresh(recipe)
    return _redirect(request, f"/recipes/{recipe.id}")


@router.post("/api/recipes/{recipe_id}")
async def recipe_form_dispatch(recipe_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    form = await request.form()
    method = str(form.get("_method", "")).upper()
    result = await db.execute(
        select(Recipe).options(selectinload(Recipe.images)).where(Recipe.id == recipe_id))
    recipe = result.scalar_one_or_none()
    if not recipe:
        raise HTTPException(404, "Recipe not found")

    if method == "DELETE":
        for img in recipe.images:
            _unlink(os.path.join(RECIPES_DIR, img.filename))
            _unlink(os.path.join(THUMBS_DIR, img.thumb_filename))
        await db.delete(recipe)
        await db.commit()
        return _redirect(request, "/recipes")

    if method == "PUT":
        data = _parse_recipe_form(form)
        if not data["title"]:
            raise HTTPException(422, "title required")
        for k, v in data.items():
            setattr(recipe, k, v)
        recipe.updated_at = _now()
        await db.commit()
        return _redirect(request, f"/recipes/{recipe.id}")

    raise HTTPException(405, "Use _method=PUT or _method=DELETE")


# ── Recipe images ─────────────────────────────────────────

@router.post("/api/recipes/{recipe_id}/images")
async def upload_recipe_image(recipe_id: int, request: Request, file: UploadFile = File(...),
                              db: AsyncSession = Depends(get_db)):
    recipe = await db.get(Recipe, recipe_id)
    if not recipe:
        raise HTTPException(404, "Recipe not found")
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in IMAGE_EXT:
        raise HTTPException(400, f"Image type '{ext}' not allowed")
    uid = uuid.uuid4().hex
    filename = f"{uid}{ext}"
    thumb_filename = f"{uid}_thumb.webp"
    full = os.path.join(RECIPES_DIR, filename)
    with open(full, "wb") as f:
        shutil.copyfileobj(file.file, f)
    try:
        _make_thumbnail(full, os.path.join(THUMBS_DIR, thumb_filename))
    except Exception:
        _unlink(full)
        raise HTTPException(400, "Could not process image")
    db.add(RecipeImage(recipe_id=recipe_id, filename=filename, thumb_filename=thumb_filename,
                       original_name=file.filename or filename))
    recipe.updated_at = _now()
    await db.commit()
    return _redirect(request, f"/recipes/{recipe_id}")


@router.post("/api/recipes/images/{image_id}/delete")
async def delete_recipe_image(image_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    img = await db.get(RecipeImage, image_id)
    if not img:
        raise HTTPException(404, "Image not found")
    recipe_id = img.recipe_id
    _unlink(os.path.join(RECIPES_DIR, img.filename))
    _unlink(os.path.join(THUMBS_DIR, img.thumb_filename))
    await db.delete(img)
    await db.commit()
    return _redirect(request, f"/recipes/{recipe_id}")


# ── Recipe categories API ─────────────────────────────────

@router.post("/api/recipe-categories")
async def create_recipe_category(request: Request, db: AsyncSession = Depends(get_db)):
    form = await request.form()
    name = str(form.get("name", "")).strip()
    if not name:
        raise HTTPException(422, "name required")
    existing = await db.execute(select(RecipeCategory).where(RecipeCategory.name == name))
    if existing.scalar_one_or_none():
        raise HTTPException(409, "Category name already exists")
    db.add(RecipeCategory(name=name, color=str(form.get("color") or "#fb923c"),
                          icon=str(form.get("icon") or "🍽️")))
    await db.commit()
    return _redirect(request, "/recipes#recipes-settings")


@router.post("/api/recipe-categories/{cat_id}")
async def recipe_category_dispatch(cat_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    form = await request.form()
    method = str(form.get("_method", "")).upper()
    cat = await db.get(RecipeCategory, cat_id)
    if not cat:
        raise HTTPException(404, "Category not found")

    if method == "DELETE":
        # Null out the category on any recipes that used it, then remove it.
        await db.execute(update(Recipe).where(Recipe.category_id == cat_id).values(category_id=None))
        await db.delete(cat)
        await db.commit()
        return _redirect(request, "/recipes#recipes-settings")

    if method == "PUT":
        name = str(form.get("name") or "").strip()
        if name:
            dupe = await db.execute(
                select(RecipeCategory).where(RecipeCategory.name == name, RecipeCategory.id != cat_id))
            if dupe.scalar_one_or_none():
                raise HTTPException(409, "Category name already exists")
            cat.name = name
        if form.get("color"):
            cat.color = str(form.get("color"))
        if form.get("icon"):
            cat.icon = str(form.get("icon"))
        await db.commit()
        return _redirect(request, "/recipes#recipes-settings")

    raise HTTPException(405, "Use _method=PUT or _method=DELETE")
