"""
Pydantic schemas for request/response validation.
"""
from typing import Optional
from pydantic import BaseModel, Field


# ── Category ──────────────────────────────────────────────

class CategoryCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    color: str = Field(default="#3b82f6", pattern=r"^#[0-9a-fA-F]{6}$")
    icon: str = Field(default="🔧", max_length=10)


class CategoryUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    color: Optional[str] = Field(None, pattern=r"^#[0-9a-fA-F]{6}$")
    icon: Optional[str] = Field(None, max_length=10)


class CategoryOut(BaseModel):
    id: int
    name: str
    color: str
    icon: str

    model_config = {"from_attributes": True}


# ── Task History ──────────────────────────────────────────

class TaskHistoryOut(BaseModel):
    id: int
    task_id: int
    event_type: str
    event_date: str

    model_config = {"from_attributes": True}


# ── Photo ─────────────────────────────────────────────────

class PhotoOut(BaseModel):
    id: int
    task_id: int
    filename: str
    original_name: str
    uploaded_at: str

    model_config = {"from_attributes": True}


# ── Task ──────────────────────────────────────────────────

class TaskCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    description: str = ""
    category_id: Optional[int] = None
    due_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    recurrence_interval: Optional[int] = Field(None, ge=1)
    recurrence_unit: Optional[str] = Field(None, pattern=r"^(day|week|month|year)$")


class TaskUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    category_id: Optional[int] = None
    due_date: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    recurrence_interval: Optional[int] = Field(None, ge=1)
    recurrence_unit: Optional[str] = Field(None, pattern=r"^(day|week|month|year)$")


class TaskOut(BaseModel):
    id: int
    title: str
    description: str = ""
    category_id: Optional[int]
    category: Optional[CategoryOut] = None
    due_date: str
    recurrence_interval: Optional[int] = None
    recurrence_unit: Optional[str] = None
    recurrence_label: Optional[str] = None
    is_active: bool
    created_at: str
    updated_at: str
    is_recurring: bool
    is_overdue: bool
    days_until_due: int
    history: list[TaskHistoryOut] = []
    photos: list[PhotoOut] = []

    model_config = {"from_attributes": True}


class TaskListOut(BaseModel):
    id: int
    title: str
    description: str = ""
    category_id: Optional[int]
    category: Optional[CategoryOut] = None
    due_date: str
    recurrence_interval: Optional[int] = None
    recurrence_unit: Optional[str] = None
    recurrence_label: Optional[str] = None
    is_active: bool
    is_recurring: bool
    is_overdue: bool
    days_until_due: int
    photos: list[PhotoOut] = []

    model_config = {"from_attributes": True}


# ── Actions ───────────────────────────────────────────────

class SnoozeRequest(BaseModel):
    days: int = Field(..., ge=1, le=365)


# ── Dashboard / Widget ────────────────────────────────────

class DueTaskResponse(BaseModel):
    count: int
    tasks: list[TaskListOut]


class WidgetStats(BaseModel):
    past: int     # overdue (strictly before today)
    today: int    # due today
    soon: int     # due in next 7 days


class AgentSummary(BaseModel):
    date: str
    due_today: list[TaskListOut]
    overdue: list[TaskListOut]
    upcoming_week: list[TaskListOut]
    total_due_today: int
    total_overdue: int
    total_upcoming_week: int
