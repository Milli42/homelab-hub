"""
Homelab Hub — FastAPI application entry point.

Modules: Dashboard (unified), Tasks (home maintenance), Notes (project notes),
Dog Vax (pet vaccines & weights), Recipes (recipe box). Each module is a router
under app/routes/.
"""
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select

from app.database import init_db, async_session
from app.models import Category, RecipeCategory


# ── Template loader ───────────────────────────────────────

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
templates = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(["html", "xml"]),
)


def date_format(value: str, fmt: str = "%b %d, %Y") -> str:
    from datetime import date
    try:
        return date.fromisoformat(value).strftime(fmt)
    except (ValueError, TypeError):
        return value


def days_from_now(value: str) -> int:
    from datetime import date
    try:
        return (date.fromisoformat(value) - date.today()).days
    except (ValueError, TypeError):
        return 0


def relative_time(value: str) -> str:
    """'2h ago' style label from a 'YYYY-MM-DD HH:MM:SS' UTC timestamp."""
    from datetime import datetime
    try:
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return value or ""
    delta = datetime.utcnow() - dt
    s = int(delta.total_seconds())
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    if s < 2592000:
        return f"{s // 86400}d ago"
    return dt.strftime("%b %d, %Y")


templates.filters["datefmt"] = date_format
templates.filters["daysfromnow"] = days_from_now
templates.filters["reltime"] = relative_time


# ── Seed data ─────────────────────────────────────────────

DEFAULT_CATEGORIES = [
    ("HVAC", "#ef4444", "🌡️"),
    ("Plumbing", "#3b82f6", "🔧"),
    ("Electrical", "#f59e0b", "⚡"),
    ("Pest Control", "#8b5cf6", "🪲"),
    ("Yard & Garden", "#22c55e", "🌿"),
    ("Appliances", "#06b6d4", "🧺"),
    ("Safety", "#f97316", "🛡️"),
    ("General", "#6b7280", "📋"),
]


async def seed_categories():
    async with async_session() as session:
        result = await session.execute(select(Category))
        if result.first() is not None:
            return
        for name, color, icon in DEFAULT_CATEGORIES:
            session.add(Category(name=name, color=color, icon=icon))
        await session.commit()


DEFAULT_RECIPE_CATEGORIES = [
    ("Mexican", "#ef4444", "🌮"),
    ("American", "#3b82f6", "🍔"),
    ("Italian", "#22c55e", "🍝"),
    ("Desserts", "#ec4899", "🍰"),
]


async def seed_recipe_categories():
    async with async_session() as session:
        result = await session.execute(select(RecipeCategory))
        if result.first() is not None:
            return
        for name, color, icon in DEFAULT_RECIPE_CATEGORIES:
            session.add(RecipeCategory(name=name, color=color, icon=icon))
        await session.commit()


# ── Auth middleware ───────────────────────────────────────

API_KEY = os.environ.get("API_KEY", "")


class ApiKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if API_KEY and request.url.path.startswith("/api/"):
            key = request.headers.get("X-API-Key", "")
            if key != API_KEY:
                return HTMLResponse(status_code=401, content="Invalid API key")
        return await call_next(request)


# ── Lifespan ──────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await seed_categories()
    await seed_recipe_categories()
    app.state.templates = templates
    yield
    from app.widgets import aclose as close_widget_clients
    await close_widget_clients()


# ── App ───────────────────────────────────────────────────

app = FastAPI(
    title="Homelab Hub",
    description="Unified home dashboard: maintenance tasks, project notes, and dog vaccine tracking.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(ApiKeyMiddleware)

# Static files
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

# Task photos
images_dir = os.environ.get("IMAGES_DIR", "data/images")
os.makedirs(images_dir, exist_ok=True)
app.mount("/images", StaticFiles(directory=images_dir), name="images")

# Note images + thumbnails
notes_dir = os.environ.get("NOTES_DIR", "data/notes")
os.makedirs(os.path.join(notes_dir, "thumbs"), exist_ok=True)
app.mount("/note-images", StaticFiles(directory=notes_dir), name="note-images")

# Recipe images + thumbnails
recipes_dir = os.environ.get("RECIPES_DIR", "data/recipes")
os.makedirs(os.path.join(recipes_dir, "thumbs"), exist_ok=True)
app.mount("/recipe-images", StaticFiles(directory=recipes_dir), name="recipe-images")

# Routes — order matters: specific page routes before parameterized ones
from app.routes.tasks import router as tasks_router, widget_router
from app.routes.images import router as images_router
from app.routes.categories import router as categories_router
from app.routes.notes import router as notes_router
from app.routes.vax import router as vax_router
from app.routes.recipes import router as recipes_router
from app.routes.bookmarks import router as bookmarks_router
from app.routes.pages import router as pages_router

app.include_router(tasks_router)
app.include_router(widget_router)
app.include_router(images_router)
app.include_router(categories_router)
app.include_router(notes_router)
app.include_router(vax_router)
app.include_router(recipes_router)
app.include_router(bookmarks_router)
app.include_router(pages_router)
