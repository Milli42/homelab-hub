"""
Page routes: server-rendered HTML views using Jinja2 + HTMX.

Tab structure:
  /          → unified Dashboard (calendar + attention feed across modules)
  /tasks     → Tasks tab (home maintenance, time-sectioned)
  /notes     → Notes tab (notes.py router)
  /dogvax    → Dog Vax tab (vax.py router)
"""
import calendar as cal
import os
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import Task, Category, Pet, Vaccine, Note, NoteGroup

router = APIRouter(tags=["pages"])


def _month_grid(month: str | None):
    """Return (first, grid_days, prev, next, label) for a Sunday-first month grid."""
    today = date.today()
    try:
        year, mon = (int(p) for p in (month or "").split("-"))
        first = date(year, mon, 1)
    except (ValueError, AttributeError):
        first = today.replace(day=1)
    prev_month = (first - timedelta(days=1)).replace(day=1)
    next_month = (first + timedelta(days=32)).replace(day=1)
    days_in_month = cal.monthrange(first.year, first.month)[1]
    last = first.replace(day=days_in_month)
    grid_start = first - timedelta(days=(first.weekday() + 1) % 7)
    grid_end = last + timedelta(days=(5 - last.weekday()) % 7)
    grid_days = []
    d = grid_start
    while d <= grid_end:
        grid_days.append(d)
        d += timedelta(days=1)
    return first, grid_days, grid_start, grid_end, prev_month.strftime("%Y-%m"), \
        next_month.strftime("%Y-%m"), first.strftime("%B %Y")


# ── Unified Dashboard ─────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, month: str | None = Query(None),
                    db: AsyncSession = Depends(get_db)):
    today = date.today()
    today_str = today.isoformat()
    week_end = (today + timedelta(days=7)).isoformat()
    soon_30 = (today + timedelta(days=30)).isoformat()

    first, grid_days, grid_start, grid_end, prev_m, next_m, month_label = _month_grid(month)

    # ── Tasks ──
    result = await db.execute(
        select(Task).options(selectinload(Task.category))
        .where(Task.is_active == 1, Task.due_date <= week_end)
        .order_by(Task.due_date.asc()))
    near_tasks = result.scalars().all()
    overdue_tasks = [t for t in near_tasks if t.due_date < today_str]
    today_tasks = [t for t in near_tasks if t.due_date == today_str]
    week_tasks = [t for t in near_tasks if t.due_date > today_str]

    # Calendar-range tasks
    result = await db.execute(
        select(Task).options(selectinload(Task.category))
        .where(Task.is_active == 1,
               Task.due_date >= grid_start.isoformat(),
               Task.due_date <= grid_end.isoformat())
        .order_by(Task.due_date.asc()))
    cal_tasks = result.scalars().all()

    # ── Vaccines ──
    result = await db.execute(
        select(Vaccine).options(selectinload(Vaccine.pet))
        .where(Vaccine.is_archived == 0).order_by(Vaccine.due_date.asc()))
    all_vax = result.scalars().all()
    overdue_vax = [v for v in all_vax if v.due_date < today_str]
    soon_vax = [v for v in all_vax if today_str <= v.due_date <= soon_30]
    cal_vax = [v for v in all_vax
               if grid_start.isoformat() <= v.due_date <= grid_end.isoformat()]

    # Pets w/ next vaccine countdown
    result = await db.execute(
        select(Pet).options(selectinload(Pet.vaccines), selectinload(Pet.weights))
        .order_by(Pet.name))
    pets = result.scalars().all()
    pet_cards = []
    for p in pets:
        active = [v for v in p.vaccines if not v.is_archived]
        future = sorted([v for v in active if v.due_date >= today_str], key=lambda v: v.due_date)
        overdue_p = sorted([v for v in active if v.due_date < today_str], key=lambda v: v.due_date)
        nxt = future[0] if future else (overdue_p[-1] if overdue_p else None)
        latest_w = max(p.weights, key=lambda w: w.date) if p.weights else None
        pet_cards.append({"pet": p, "next_vax": nxt, "latest_weight": latest_w,
                          "overdue_count": len(overdue_p)})

    # ── Notes ──
    result = await db.execute(
        select(Note).options(selectinload(Note.group), selectinload(Note.images))
        .order_by(Note.updated_at.desc()).limit(4))
    recent_notes = result.scalars().all()
    notes_count = await db.scalar(select(func.count(Note.id))) or 0

    # ── Calendar day map: {iso: {"tasks": [...], "vax": [...]}} ──
    cal_by_day: dict[str, dict] = {}
    for t in cal_tasks:
        cal_by_day.setdefault(t.due_date, {"tasks": [], "vax": []})["tasks"].append(t)
    for v in cal_vax:
        cal_by_day.setdefault(v.due_date, {"tasks": [], "vax": []})["vax"].append(v)

    # JSON payload for the day-detail popover
    import json
    cal_json = json.dumps({
        iso: (
            [{"kind": "task", "label": t.title,
              "icon": (t.category.icon if t.category else "📋"),
              "color": (t.category.color if t.category else "#60a5fa"),
              "url": f"/tasks/{t.id}"} for t in day["tasks"]]
            + [{"kind": "vax", "label": f"{v.pet.name}: {v.vaccine_name}",
                "icon": "🐾", "color": "#fb923c",
                "url": f"/dogvax?pet={v.pet_id}"} for v in day["vax"]]
        )
        for iso, day in cal_by_day.items()
    }).replace("</", "<\\/")  # harden against </script> breakout; rendered with |safe

    # Agenda: upcoming 14 days combined feed
    agenda_end = (today + timedelta(days=14)).isoformat()
    agenda = []
    for t in near_tasks:
        if today_str <= t.due_date <= agenda_end:
            agenda.append({"kind": "task", "date": t.due_date, "obj": t})
    for v in all_vax:
        if today_str <= v.due_date <= agenda_end:
            agenda.append({"kind": "vax", "date": v.due_date, "obj": v})
    agenda.sort(key=lambda x: x["date"])

    return HTMLResponse(request.app.state.templates.get_template("dashboard.html").render(
        request=request, today=today, today_str=today_str,
        overdue_tasks=overdue_tasks, today_tasks=today_tasks, week_tasks=week_tasks,
        overdue_vax=overdue_vax, soon_vax=soon_vax,
        pet_cards=pet_cards, recent_notes=recent_notes, notes_count=notes_count,
        grid_days=grid_days, cal_by_day=cal_by_day, first=first,
        prev_month=prev_m, next_month=next_m, month_label=month_label,
        agenda=agenda, cal_json=cal_json,
    ))


# ── Bookmarks tab (embedded Homepage dashboard) ───────────

# URL of the Homepage dashboard to embed. Use the reverse-proxy HTTPS hostname so
# the iframe works both on-LAN and remotely and avoids mixed-content blocking when
# the Hub itself is served over https. Override per-deployment with HOMEPAGE_URL.
HOMEPAGE_URL = os.environ.get("HOMEPAGE_URL", "https://bookmarks.mjaranch.com")


@router.get("/bookmarks", response_class=HTMLResponse)
async def bookmarks_page(request: Request):
    return HTMLResponse(request.app.state.templates.get_template("bookmarks.html").render(
        request=request, homepage_url=HOMEPAGE_URL))


# ── Tasks tab ─────────────────────────────────────────────

@router.get("/tasks", response_class=HTMLResponse)
async def tasks_page(
    request: Request,
    category_id: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    today = date.today()
    today_str = today.isoformat()
    week_end = (today + timedelta(days=7)).isoformat()
    month_end = (today + timedelta(days=30)).isoformat()

    cat_filter: int | None = None
    if category_id and category_id.strip():
        try:
            cat_filter = int(category_id)
        except ValueError:
            pass

    def _base():
        q = select(Task).options(selectinload(Task.category), selectinload(Task.photos))
        if cat_filter is not None:
            q = q.where(Task.category_id == cat_filter)
        return q

    overdue = (await db.execute(
        _base().where(Task.is_active == 1, Task.due_date < today_str).order_by(Task.due_date.asc()))).scalars().all()
    due_today = (await db.execute(
        _base().where(Task.is_active == 1, Task.due_date == today_str).order_by(Task.due_date.asc()))).scalars().all()
    upcoming = (await db.execute(
        _base().where(Task.is_active == 1, Task.due_date > today_str, Task.due_date <= week_end).order_by(Task.due_date.asc()))).scalars().all()
    month = (await db.execute(
        _base().where(Task.is_active == 1, Task.due_date > week_end, Task.due_date <= month_end).order_by(Task.due_date.asc()))).scalars().all()
    future = (await db.execute(
        _base().where(Task.is_active == 1, Task.due_date > month_end).order_by(Task.due_date.asc()))).scalars().all()
    archived = (await db.execute(
        _base().where(Task.is_active == 0).order_by(Task.due_date.desc()).limit(50))).scalars().all()
    categories = (await db.execute(select(Category).order_by(Category.name))).scalars().all()

    return HTMLResponse(request.app.state.templates.get_template("tasks.html").render(
        request=request, overdue=overdue, due_today=due_today, upcoming=upcoming,
        month=month, future=future, archived=archived, today_str=today_str,
        categories=categories, selected_category=cat_filter,
    ))


@router.get("/tasks/new", response_class=HTMLResponse)
async def task_form(request: Request, db: AsyncSession = Depends(get_db)):
    categories = (await db.execute(select(Category).order_by(Category.name))).scalars().all()
    return HTMLResponse(request.app.state.templates.get_template("task_form.html").render(
        request=request, task=None, categories=categories))


@router.get("/tasks/{task_id}", response_class=HTMLResponse)
async def task_detail(request: Request, task_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Task)
        .options(selectinload(Task.category), selectinload(Task.history), selectinload(Task.photos))
        .where(Task.id == task_id))
    task = result.scalar_one_or_none()
    if not task:
        return HTMLResponse("<div class='p-8 text-center text-red-400'>Task not found</div>", status_code=404)
    categories = (await db.execute(select(Category).order_by(Category.name))).scalars().all()
    return HTMLResponse(request.app.state.templates.get_template("task_detail.html").render(
        request=request, task=task, categories=categories))
