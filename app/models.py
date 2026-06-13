"""
SQLAlchemy ORM models for home maintenance tracker.
"""
from datetime import date, datetime, timedelta
from dateutil.relativedelta import relativedelta
from sqlalchemy import (
    Column, Integer, Float, String, Text, ForeignKey, CheckConstraint, Index
)
from sqlalchemy.orm import relationship
from app.database import Base

# Conversion factors to days (approximate for month/year)
UNIT_TO_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}


class Category(Base):
    __tablename__ = "categories"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), unique=True, nullable=False)
    color = Column(String(7), default="#3b82f6")   # hex color
    icon = Column(String(10), default="🔧")

    tasks = relationship("Task", back_populates="category")


class Task(Base):
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True)
    title = Column(String(255), nullable=False)
    description = Column(Text, default="")
    category_id = Column(Integer, ForeignKey("categories.id"))
    due_date = Column(String(10), nullable=False)        # YYYY-MM-DD
    recurrence_interval = Column(Integer, nullable=True)  # e.g. 3
    recurrence_unit = Column(String(10), nullable=True)   # day, week, month, year
    is_active = Column(Integer, default=1)                # soft delete
    created_at = Column(String(19), default=lambda: datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))
    updated_at = Column(String(19), default=lambda: datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))

    category = relationship("Category", back_populates="tasks")
    history = relationship("TaskHistory", back_populates="task", order_by="TaskHistory.event_date.desc()")
    photos = relationship("Photo", back_populates="task", order_by="Photo.uploaded_at.desc()")

    __table_args__ = (
        Index("idx_tasks_due_date", "due_date"),
        Index("idx_tasks_category", "category_id"),
        Index("idx_tasks_active_due", "is_active", "due_date"),
        CheckConstraint(
            "recurrence_unit IS NULL OR recurrence_unit IN ('day','week','month','year')",
            name="ck_recurrence_unit"
        ),
    )

    @property
    def recurrence_days(self) -> int | None:
        """Convert interval+unit to days for backward compatibility."""
        if self.recurrence_interval is None or self.recurrence_unit is None:
            return None
        return self.recurrence_interval * UNIT_TO_DAYS.get(self.recurrence_unit, 1)

    @property
    def is_recurring(self) -> bool:
        return self.recurrence_interval is not None and self.recurrence_unit is not None

    @property
    def is_overdue(self) -> bool:
        return date.today() > date.fromisoformat(self.due_date)

    @property
    def days_until_due(self) -> int:
        return (date.fromisoformat(self.due_date) - date.today()).days

    @property
    def recurrence_label(self) -> str | None:
        """Human-readable recurrence label."""
        if not self.is_recurring:
            return None
        unit = self.recurrence_unit
        if self.recurrence_interval == 1:
            unit = unit.rstrip('s')  # "1 week" not "1 weeks"
        else:
            unit = unit + 's' if not unit.endswith('s') else unit
        return f"every {self.recurrence_interval} {unit}"


def advance_due_date(due_date_str: str, interval: int | None, unit: str | None) -> str | None:
    """Advance due date by recurrence. Returns None for one-time tasks."""
    if interval is None or unit is None:
        return None
    due = date.fromisoformat(due_date_str)
    today = date.today()

    if unit == "day":
        delta = timedelta(days=interval)
    elif unit == "week":
        delta = timedelta(weeks=interval)
    elif unit == "month":
        delta = relativedelta(months=interval)
    elif unit == "year":
        delta = relativedelta(years=interval)
    else:
        return None

    # Always advance at least one interval, then skip past today if needed
    due += delta
    while due <= today:
        due += delta
    return due.isoformat()


class TaskHistory(Base):
    __tablename__ = "task_history"

    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=False)
    event_type = Column(String(10), nullable=False)     # 'completed' or 'skipped'
    event_date = Column(String(10), nullable=False)      # YYYY-MM-DD
    created_at = Column(String(19), default=lambda: datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))

    task = relationship("Task", back_populates="history")

    __table_args__ = (
        CheckConstraint("event_type IN ('completed', 'skipped')", name="ck_event_type"),
        Index("idx_history_task", "task_id"),
    )


class Photo(Base):
    __tablename__ = "photos"

    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=False)
    filename = Column(String(255), nullable=False)       # UUID filename on disk
    original_name = Column(String(255), nullable=False)  # original upload name
    uploaded_at = Column(String(19), default=lambda: datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))

    task = relationship("Task", back_populates="photos")

    __table_args__ = (
        Index("idx_photos_task", "task_id"),
    )


# ════════════════════════════════════════════════════════════
# Dog Vax module
# ════════════════════════════════════════════════════════════

class Pet(Base):
    __tablename__ = "pets"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    species = Column(String(30), default="dog")
    breed = Column(String(100), nullable=True)
    birth_date = Column(String(10), nullable=True)        # YYYY-MM-DD
    avatar_path = Column(String(255), nullable=True)
    created_at = Column(String(19), default=lambda: datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))

    vaccines = relationship("Vaccine", back_populates="pet", cascade="all, delete-orphan",
                            order_by="Vaccine.due_date.asc()")
    weights = relationship("WeightLog", back_populates="pet", cascade="all, delete-orphan",
                           order_by="WeightLog.date.desc()")

    @property
    def age_label(self) -> str | None:
        if not self.birth_date:
            return None
        try:
            bd = date.fromisoformat(self.birth_date)
        except ValueError:
            return None
        today = date.today()
        years = today.year - bd.year - ((today.month, today.day) < (bd.month, bd.day))
        months = (today.year - bd.year) * 12 + today.month - bd.month - (today.day < bd.day)
        if years >= 1:
            return f"{years}y {months - years * 12}m"
        return f"{months}m"


class Vaccine(Base):
    __tablename__ = "vaccines"

    id = Column(Integer, primary_key=True)
    pet_id = Column(Integer, ForeignKey("pets.id", ondelete="CASCADE"), nullable=False)
    vaccine_name = Column(String(150), nullable=False)
    administered_date = Column(String(10), nullable=False)
    due_date = Column(String(10), nullable=False)
    file_path = Column(String(255), nullable=True)
    notes = Column(Text, nullable=True)
    is_archived = Column(Integer, default=0)
    created_at = Column(String(19), default=lambda: datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))

    pet = relationship("Pet", back_populates="vaccines")

    __table_args__ = (
        Index("idx_vaccines_pet", "pet_id"),
        Index("idx_vaccines_due", "due_date"),
    )

    @property
    def days_until_due(self) -> int:
        return (date.fromisoformat(self.due_date) - date.today()).days

    @property
    def is_overdue(self) -> bool:
        return self.days_until_due < 0


class WeightLog(Base):
    __tablename__ = "weight_logs"

    id = Column(Integer, primary_key=True)
    pet_id = Column(Integer, ForeignKey("pets.id", ondelete="CASCADE"), nullable=False)
    weight = Column(Float, nullable=False)
    date = Column(String(10), nullable=False)
    notes = Column(Text, nullable=True)
    created_at = Column(String(19), default=lambda: datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))

    pet = relationship("Pet", back_populates="weights")

    __table_args__ = (
        Index("idx_weights_pet", "pet_id"),
    )


class Setting(Base):
    __tablename__ = "settings"

    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=False)


# ════════════════════════════════════════════════════════════
# Notes module
# ════════════════════════════════════════════════════════════

class NoteGroup(Base):
    __tablename__ = "note_groups"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    color = Column(String(7), default="#a78bfa")
    icon = Column(String(10), default="📁")
    sort_order = Column(Integer, default=0)
    pin_hash = Column(String(160), nullable=True)   # null = unlocked; salted PBKDF2 hash of a 6-digit PIN
    created_at = Column(String(19), default=lambda: datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))

    notes = relationship("Note", back_populates="group", cascade="all, delete-orphan",
                         order_by="Note.is_pinned.desc(), Note.updated_at.desc()")

    @property
    def is_locked(self) -> bool:
        return bool(self.pin_hash)


class Note(Base):
    __tablename__ = "notes"

    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey("note_groups.id", ondelete="CASCADE"), nullable=False)
    title = Column(String(255), nullable=False)
    body = Column(Text, default="")          # markdown-ish: "- [ ]" / "- [x]" checkbox lines
    is_pinned = Column(Integer, default=0)
    created_at = Column(String(19), default=lambda: datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))
    updated_at = Column(String(19), default=lambda: datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))

    group = relationship("NoteGroup", back_populates="notes")
    images = relationship("NoteImage", back_populates="note", cascade="all, delete-orphan",
                          order_by="NoteImage.uploaded_at.desc()")

    __table_args__ = (
        Index("idx_notes_group", "group_id"),
    )

    @property
    def checkbox_stats(self) -> tuple[int, int]:
        """(checked, total) checkbox counts in the body."""
        import re
        boxes = re.findall(r"^\s*[-*] \[([ xX])\]", self.body or "", re.MULTILINE)
        total = len(boxes)
        checked = sum(1 for b in boxes if b.lower() == "x")
        return checked, total

    @property
    def preview(self) -> str:
        """Plain-text preview of the body (checkboxes stripped)."""
        import re
        text = re.sub(r"^\s*[-*] \[[ xX]\]\s*", "• ", self.body or "", flags=re.MULTILINE)
        text = re.sub(r"[#*_`>]+", "", text)
        return text.strip()[:160]


class NoteImage(Base):
    __tablename__ = "note_images"

    id = Column(Integer, primary_key=True)
    note_id = Column(Integer, ForeignKey("notes.id", ondelete="CASCADE"), nullable=False)
    filename = Column(String(255), nullable=False)        # UUID name on disk
    thumb_filename = Column(String(255), nullable=False)  # generated thumbnail
    original_name = Column(String(255), nullable=False)
    uploaded_at = Column(String(19), default=lambda: datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))

    note = relationship("Note", back_populates="images")

    __table_args__ = (
        Index("idx_note_images_note", "note_id"),
    )


# ════════════════════════════════════════════════════════════
# Recipes module
# ════════════════════════════════════════════════════════════

class RecipeCategory(Base):
    __tablename__ = "recipe_categories"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), unique=True, nullable=False)
    color = Column(String(7), default="#fb923c")   # hex color
    icon = Column(String(10), default="🍽️")

    recipes = relationship("Recipe", back_populates="category")


class Recipe(Base):
    __tablename__ = "recipes"

    id = Column(Integer, primary_key=True)
    title = Column(String(255), nullable=False)
    category_id = Column(Integer, ForeignKey("recipe_categories.id"), nullable=True)
    prep_time = Column(String(40), nullable=True)         # free text, e.g. "15 min"
    cook_time = Column(String(40), nullable=True)         # free text, e.g. "45 min"
    ingredients = Column(Text, default="")               # one per line
    directions = Column(Text, default="")                # one step per line
    created_at = Column(String(19), default=lambda: datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))
    updated_at = Column(String(19), default=lambda: datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))

    category = relationship("RecipeCategory", back_populates="recipes")
    images = relationship("RecipeImage", back_populates="recipe", cascade="all, delete-orphan",
                          order_by="RecipeImage.uploaded_at.asc()")

    __table_args__ = (
        Index("idx_recipes_category", "category_id"),
    )

    @property
    def main_image(self):
        """First uploaded image — used as the card/hero thumbnail."""
        return self.images[0] if self.images else None

    @property
    def ingredient_list(self) -> list[str]:
        """Non-empty ingredient lines."""
        return [ln.strip() for ln in (self.ingredients or "").splitlines() if ln.strip()]

    @property
    def direction_steps(self) -> list[str]:
        """Non-empty direction lines, in order."""
        return [ln.strip() for ln in (self.directions or "").splitlines() if ln.strip()]


class RecipeImage(Base):
    __tablename__ = "recipe_images"

    id = Column(Integer, primary_key=True)
    recipe_id = Column(Integer, ForeignKey("recipes.id", ondelete="CASCADE"), nullable=False)
    filename = Column(String(255), nullable=False)        # UUID name on disk
    thumb_filename = Column(String(255), nullable=False)  # generated thumbnail
    original_name = Column(String(255), nullable=False)
    uploaded_at = Column(String(19), default=lambda: datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))

    recipe = relationship("Recipe", back_populates="images")

    __table_args__ = (
        Index("idx_recipe_images_recipe", "recipe_id"),
    )


# ════════════════════════════════════════════════════════════
# Bookmarks module
# ════════════════════════════════════════════════════════════

class BookmarkGroup(Base):
    """A category of bookmarks (e.g. Media, Network), rendered as a section."""
    __tablename__ = "bookmark_groups"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    color = Column(String(7), default="#60a5fa")   # hex accent for the section/tiles
    icon = Column(String(10), default="🔖")
    columns = Column(Integer, default=0)           # 0 = auto-fill; else fixed tiles/row on desktop
    sort_order = Column(Integer, default=0)
    created_at = Column(String(19), default=lambda: datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))

    bookmarks = relationship("Bookmark", back_populates="group", cascade="all, delete-orphan",
                             order_by="Bookmark.sort_order, Bookmark.id")


class Bookmark(Base):
    __tablename__ = "bookmarks"

    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey("bookmark_groups.id", ondelete="CASCADE"), nullable=False)
    title = Column(String(150), nullable=False)
    url = Column(String(2048), nullable=False)
    subtitle = Column(String(255), nullable=True)   # optional one-liner under the title
    # Smart icon: an emoji, or an image URL (http(s):// or root-relative /path).
    # Empty → a lettered badge is rendered from the title.
    icon = Column(String(512), default="")
    # Tile width as a span of a 12-column grid: 2=⅙ 3=¼ 4=⅓ 6=½ 8=⅔ 12=full row.
    width = Column(Integer, default=4)
    sort_order = Column(Integer, default=0)
    created_at = Column(String(19), default=lambda: datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))

    group = relationship("BookmarkGroup", back_populates="bookmarks")

    __table_args__ = (
        Index("idx_bookmarks_group", "group_id"),
    )

    @property
    def icon_is_image(self) -> bool:
        """True when `icon` should render as an <img> rather than emoji/text."""
        return (self.icon or "").strip().startswith(("http://", "https://", "/"))

    @property
    def letter(self) -> str:
        """First letter of the title — fallback badge when no icon is set."""
        return ((self.title or "?").strip()[:1] or "?").upper()
