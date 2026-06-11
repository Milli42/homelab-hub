"""
Photo upload and serve routes.
"""
import os
import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Request
from fastapi.responses import FileResponse, RedirectResponse, HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Task, Photo

IMAGES_DIR = os.environ.get("IMAGES_DIR", "data/images")
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

router = APIRouter(prefix="/api/tasks/{task_id}/images", tags=["images"])


def _redirect_photo(request: Request, task_id: int) -> RedirectResponse | HTMLResponse:
    """Redirect back to the task detail page."""
    url = f"/tasks/{task_id}"
    if request.headers.get("HX-Request"):
        return HTMLResponse(status_code=200, headers={"HX-Redirect": url})
    return RedirectResponse(url=url, status_code=303)


@router.post("")
async def upload_image(
    task_id: int,
    request: Request,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    # Verify task exists
    result = await db.execute(select(Task).where(Task.id == task_id, Task.is_active == 1))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Task not found")

    # Validate extension
    _, ext = os.path.splitext(file.filename or "")
    if ext.lower() not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {ext}")

    # Generate unique filename
    unique_name = f"{uuid.uuid4().hex}{ext}"
    os.makedirs(IMAGES_DIR, exist_ok=True)
    file_path = os.path.join(IMAGES_DIR, unique_name)

    # Write to disk
    content = await file.read()
    with open(file_path, "wb") as f:
        f.write(content)

    # Record in DB
    photo = Photo(
        task_id=task_id,
        filename=unique_name,
        original_name=file.filename or "unknown",
    )
    db.add(photo)
    await db.commit()

    return _redirect_photo(request, task_id)


@router.get("/{photo_id}")
async def serve_image(task_id: int, photo_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Photo).where(Photo.id == photo_id, Photo.task_id == task_id))
    photo = result.scalar_one_or_none()
    if not photo:
        raise HTTPException(status_code=404, detail="Photo not found")

    file_path = os.path.join(IMAGES_DIR, photo.filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Photo file not found on disk")

    return FileResponse(file_path)


@router.delete("/{photo_id}")
@router.post("/{photo_id}")
async def delete_image(task_id: int, photo_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    if request.method == "POST":
        form = await request.form()
        if form.get("_method", "").upper() != "DELETE":
            raise HTTPException(status_code=405, detail="Use DELETE or POST with _method=DELETE")
    result = await db.execute(select(Photo).where(Photo.id == photo_id, Photo.task_id == task_id))
    photo = result.scalar_one_or_none()
    if not photo:
        raise HTTPException(status_code=404, detail="Photo not found")

    # Remove from disk (best effort)
    file_path = os.path.join(IMAGES_DIR, photo.filename)
    try:
        os.remove(file_path)
    except OSError:
        pass

    await db.delete(photo)
    await db.commit()

    return _redirect_photo(request, task_id)
