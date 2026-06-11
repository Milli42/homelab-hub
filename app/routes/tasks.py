"""
Task CRUD routes + complete/skip/snooze actions.
"""
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select, func, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
import os

from app.database import get_db
from app.models import Task, TaskHistory, Category, Photo, advance_due_date
from app.schemas import (
    TaskCreate, TaskUpdate, TaskOut, TaskListOut,
    SnoozeRequest, DueTaskResponse, WidgetStats, AgentSummary,
)

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


# ── Helpers ───────────────────────────────────────────────

def _redirect(request: Request, url: str) -> RedirectResponse | HTMLResponse:
    if request.headers.get("HX-Request"):
        return HTMLResponse(status_code=200, headers={"HX-Redirect": url})
    return RedirectResponse(url=url, status_code=303)


async def _get_task_or_404(db: AsyncSession, task_id: int) -> Task:
    result = await db.execute(
        select(Task)
        .options(selectinload(Task.category), selectinload(Task.history), selectinload(Task.photos))
        .where(Task.id == task_id, Task.is_active == 1)
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


async def _get_any_task_or_404(db: AsyncSession, task_id: int) -> Task:
    """Like _get_task_or_404 but finds archived tasks too."""
    result = await db.execute(
        select(Task)
        .options(selectinload(Task.category), selectinload(Task.history), selectinload(Task.photos))
        .where(Task.id == task_id)
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


IMAGES_DIR = os.environ.get("IMAGES_DIR", "data/images")


async def _hard_delete_task(db: AsyncSession, task: Task):
    """Permanently delete a task and all associated records + files."""
    # Delete photos from disk
    for photo in task.photos:
        file_path = os.path.join(IMAGES_DIR, photo.filename)
        try:
            os.remove(file_path)
        except OSError:
            pass
    # Delete history, photos, then task from DB
    await db.execute(delete(TaskHistory).where(TaskHistory.task_id == task.id))
    await db.execute(delete(Photo).where(Photo.task_id == task.id))
    await db.delete(task)
    await db.commit()


def _serialize_task(task: Task, include_details: bool = False) -> dict:
    data = {
        "id": task.id, "title": task.title,
        "description": task.description or "",
        "category_id": task.category_id, "category": None,
        "due_date": task.due_date,
        "recurrence_interval": task.recurrence_interval,
        "recurrence_unit": task.recurrence_unit,
        "recurrence_label": task.recurrence_label,
        "is_active": bool(task.is_active),
        "is_recurring": task.is_recurring,
        "is_overdue": task.is_overdue,
        "days_until_due": task.days_until_due,
        "photos": [
            {"id": p.id, "task_id": p.task_id, "filename": p.filename,
             "original_name": p.original_name, "uploaded_at": p.uploaded_at}
            for p in (task.photos or [])
        ],
    }
    if task.category:
        data["category"] = {"id": task.category.id, "name": task.category.name,
                           "color": task.category.color, "icon": task.category.icon}
    if include_details:
        data.update({
            "created_at": task.created_at, "updated_at": task.updated_at,
            "history": [
                {"id": h.id, "task_id": h.task_id, "event_type": h.event_type, "event_date": h.event_date}
                for h in (task.history or [])
            ],
        })
    return data


def _parse_int(s: str | None) -> int | None:
    if s is None or str(s).strip() == "":
        return None
    return int(s)


# ── List / Filter ─────────────────────────────────────────

@router.get("", response_model=list[TaskListOut])
async def list_tasks(
    status: Optional[str] = Query(None),
    category_id: Optional[int] = None,
    due_before: Optional[str] = Query(None),
    due_after: Optional[str] = Query(None),
    search: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    query = select(Task).options(selectinload(Task.category), selectinload(Task.photos))
    if status == "completed": query = query.where(Task.is_active == 0)
    elif status != "all": query = query.where(Task.is_active == 1)
    if category_id is not None: query = query.where(Task.category_id == category_id)
    if due_before: query = query.where(Task.due_date <= due_before)
    if due_after: query = query.where(Task.due_date >= due_after)
    if search: query = query.where(Task.title.ilike(f"%{search}%"))
    query = query.order_by(Task.due_date.asc())
    result = await db.execute(query)
    return [_serialize_task(t) for t in result.scalars().all()]


# ── Detail ────────────────────────────────────────────────

@router.get("/{task_id}", response_model=TaskOut)
async def get_task(task_id: int, db: AsyncSession = Depends(get_db)):
    return _serialize_task(await _get_task_or_404(db, task_id), include_details=True)


# ── Create ────────────────────────────────────────────────

@router.post("", status_code=201)
async def create_task(request: Request, db: AsyncSession = Depends(get_db)):
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        body = await request.json()
        title, description = body["title"], body.get("description", "")
        category_id = body.get("category_id")
        due_date = body["due_date"]
        ri = body.get("recurrence_interval")
        ru = body.get("recurrence_unit")
    else:
        form = await request.form()
        title = form.get("title")
        description = form.get("description", "")
        category_id = _parse_int(form.get("category_id"))
        due_date = form.get("due_date")
        ri = _parse_int(form.get("recurrence_interval"))
        ru = form.get("recurrence_unit") or None
    if not title or not due_date:
        raise HTTPException(status_code=422, detail="title and due_date required")
    # If no interval, it's a one-time task — clear unit too
    if ri is None:
        ru = None
    elif ru is None:
        ru = "day"
    task = Task(title=title, description=description, category_id=category_id,
                due_date=due_date, recurrence_interval=ri, recurrence_unit=ru)
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return _redirect(request, f"/tasks/{task.id}")


# ── Complete (MUST be before generic /{task_id} handler) ──

@router.post("/{task_id}/complete")
async def complete_task(task_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    task = await _get_task_or_404(db, task_id)
    today = date.today().isoformat()
    db.add(TaskHistory(task_id=task.id, event_type="completed", event_date=today))
    new_due = advance_due_date(task.due_date, task.recurrence_interval, task.recurrence_unit)
    if new_due: task.due_date = new_due
    else: task.is_active = 0
    await db.commit()
    return _redirect(request, f"/tasks/{task.id}")


# ── Skip ──────────────────────────────────────────────────

@router.post("/{task_id}/skip")
async def skip_task(task_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    task = await _get_task_or_404(db, task_id)
    today = date.today().isoformat()
    db.add(TaskHistory(task_id=task.id, event_type="skipped", event_date=today))
    if task.recurrence_interval:
        task.due_date = advance_due_date(task.due_date, task.recurrence_interval, task.recurrence_unit)
    else:
        task.is_active = 0
    await db.commit()
    return _redirect(request, f"/tasks/{task.id}")


# ── Snooze ────────────────────────────────────────────────

@router.post("/{task_id}/snooze")
async def snooze_task(task_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    task = await _get_task_or_404(db, task_id)
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        days = (await request.json()).get("days", 7)
    else:
        days = int((await request.form()).get("days", 7))
    task.due_date = (date.fromisoformat(task.due_date) + timedelta(days=days)).isoformat()
    await db.commit()
    return _redirect(request, f"/tasks/{task.id}")


# ── Restore (reactivate archived task) ────────────────────

@router.post("/{task_id}/restore")
async def restore_task(task_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    # Allow restoring any task, not just active ones
    result = await db.execute(
        select(Task).options(selectinload(Task.category), selectinload(Task.history), selectinload(Task.photos))
        .where(Task.id == task_id)
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        body = await request.json()
        new_due = body.get("due_date")
    else:
        form = await request.form()
        new_due = form.get("due_date")

    if not new_due:
        raise HTTPException(status_code=422, detail="due_date is required for restore")

    task.due_date = str(new_due)
    task.is_active = 1
    task.recurrence_interval = None
    task.recurrence_unit = None
    from datetime import datetime
    task.updated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    await db.commit()
    return _redirect(request, f"/tasks/{task.id}")


# ── Generic POST /{task_id} (form _method override) ───────

@router.post("/{task_id}")
async def handle_form_post(task_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    form = await request.form()
    method = str(form.get("_method", "")).upper()

    if method == "DELETE":
        task = await _get_any_task_or_404(db, task_id)
        if task.is_active:
            task.is_active = 0
            await db.commit()
        else:
            await _hard_delete_task(db, task)
        return _redirect(request, "/tasks")

    if method == "PUT":
        task = await _get_task_or_404(db, task_id)
        t = str(form.get("title", ""))
        if t: task.title = t
        d = form.get("description")
        if d is not None: task.description = str(d)
        task.category_id = _parse_int(form.get("category_id"))
        dd = form.get("due_date")
        if dd: task.due_date = str(dd)
        task.recurrence_interval = _parse_int(form.get("recurrence_interval"))
        ru = form.get("recurrence_unit")
        # If no interval, force one-time; if interval but no unit, default to day
        if task.recurrence_interval is None:
            task.recurrence_unit = None
        else:
            task.recurrence_unit = str(ru) if ru else "day"
        from datetime import datetime
        task.updated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        await db.commit()
        return _redirect(request, f"/tasks/{task.id}")

    raise HTTPException(status_code=405, detail="Use _method=PUT or _method=DELETE")


# ── JSON PUT ──────────────────────────────────────────────

@router.put("/{task_id}")
async def update_task_json(task_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    task = await _get_task_or_404(db, task_id)
    body = TaskUpdate(**(await request.json()))
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(task, key, value)
    from datetime import datetime
    task.updated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    await db.commit()
    return _serialize_task(task, include_details=True)


# ── JSON DELETE ───────────────────────────────────────────

@router.delete("/{task_id}")
async def delete_task_json(task_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    task = await _get_any_task_or_404(db, task_id)
    if task.is_active:
        task.is_active = 0
        await db.commit()
    else:
        await _hard_delete_task(db, task)
    return _redirect(request, "/tasks")


# ── Due: Today / Week ─────────────────────────────────────

@router.get("/due/today", response_model=DueTaskResponse)
async def due_today(db: AsyncSession = Depends(get_db)):
    today = date.today().isoformat()
    result = await db.execute(
        select(Task).options(selectinload(Task.category), selectinload(Task.photos))
        .where(Task.is_active == 1, Task.due_date <= today).order_by(Task.due_date.asc()))
    tasks = result.scalars().all()
    return {"count": len(tasks), "tasks": [_serialize_task(t) for t in tasks]}


@router.get("/due/week", response_model=DueTaskResponse)
async def due_this_week(db: AsyncSession = Depends(get_db)):
    today = date.today()
    today_str, week_end = today.isoformat(), (today + timedelta(days=7)).isoformat()
    result = await db.execute(
        select(Task).options(selectinload(Task.category), selectinload(Task.photos))
        .where(Task.is_active == 1, Task.due_date >= today_str, Task.due_date <= week_end)
        .order_by(Task.due_date.asc()))
    tasks = result.scalars().all()
    return {"count": len(tasks), "tasks": [_serialize_task(t) for t in tasks]}


# ── Widget Stats ──────────────────────────────────────────

widget_router = APIRouter(tags=["dashboard"])
@widget_router.get("/api/widget/stats", response_model=WidgetStats)
async def widget_stats(db: AsyncSession = Depends(get_db)):
    today = date.today().isoformat()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    week_end = (date.today() + timedelta(days=7)).isoformat()

    past = await db.scalar(
        select(func.count(Task.id)).where(Task.is_active == 1, Task.due_date < today)) or 0
    today_count = await db.scalar(
        select(func.count(Task.id)).where(Task.is_active == 1, Task.due_date == today)) or 0
    soon = await db.scalar(
        select(func.count(Task.id)).where(
            Task.is_active == 1, Task.due_date >= tomorrow, Task.due_date <= week_end)) or 0
    return {"past": past, "today": today_count, "soon": soon}


# ── Agent Daily Summary ───────────────────────────────────

@widget_router.get("/api/agent/daily-summary", response_model=AgentSummary)
async def agent_daily_summary(db: AsyncSession = Depends(get_db)):
    today, today_str = date.today(), date.today().isoformat()
    week_end = today + timedelta(days=7)
    result = await db.execute(
        select(Task).options(selectinload(Task.category), selectinload(Task.photos))
        .where(Task.is_active == 1, Task.due_date <= today_str).order_by(Task.due_date.asc()))
    all_due = result.scalars().all()
    overdue = [t for t in all_due if t.due_date < today_str]
    due_today_list = [t for t in all_due if t.due_date == today_str]
    tomorrow = (today + timedelta(days=1)).isoformat()
    result = await db.execute(
        select(Task).options(selectinload(Task.category), selectinload(Task.photos))
        .where(Task.is_active == 1, Task.due_date >= tomorrow, Task.due_date <= week_end.isoformat())
        .order_by(Task.due_date.asc()))
    upcoming = result.scalars().all()
    return {
        "date": today_str,
        "due_today": [_serialize_task(t) for t in due_today_list],
        "overdue": [_serialize_task(t) for t in overdue],
        "upcoming_week": [_serialize_task(t) for t in upcoming],
        "total_due_today": len(due_today_list), "total_overdue": len(overdue),
        "total_upcoming_week": len(upcoming),
    }
