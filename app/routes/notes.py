"""
Notes module — groups (folders), notes with interactive checkboxes, photo
attachments with server-generated thumbnails.
"""
import os
import re
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import NoteGroup, Note, NoteImage

router = APIRouter(tags=["notes"])

NOTES_DIR = os.environ.get("NOTES_DIR", "data/notes")
THUMBS_DIR = os.path.join(NOTES_DIR, "thumbs")
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
THUMB_SIZE = 400

os.makedirs(NOTES_DIR, exist_ok=True)
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
        im.save(thumb_path, "WEBP", quality=80)


def render_note_body(body: str) -> str:
    """Render note body to HTML: checkboxes, headings, bold/italic, lists, links."""
    import html as html_mod
    lines = (body or "").split("\n")
    out = []
    cb_index = 0
    for line in lines:
        esc = html_mod.escape(line)
        m = re.match(r"^(\s*)[-*] \[([ xX])\]\s?(.*)$", line)
        if m:
            checked = m.group(2).lower() == "x"
            text = _inline(html_mod.escape(m.group(3)))
            out.append(
                f'<label class="note-check {"done" if checked else ""}">'
                f'<input type="checkbox" data-cb-index="{cb_index}" {"checked" if checked else ""}>'
                f'<span>{text}</span></label>')
            cb_index += 1
            continue
        h = re.match(r"^(#{1,3})\s+(.*)$", line)
        if h:
            lvl = len(h.group(1))
            out.append(f'<div class="note-h{lvl}">{_inline(html_mod.escape(h.group(2)))}</div>')
            continue
        b = re.match(r"^(\s*)[-*]\s+(.*)$", line)
        if b:
            out.append(f'<div class="note-li">•&nbsp;{_inline(html_mod.escape(b.group(2)))}</div>')
            continue
        if line.strip() == "":
            out.append('<div class="note-blank"></div>')
        else:
            out.append(f'<div class="note-p">{_inline(esc)}</div>')
    return "\n".join(out)


def _inline(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"(https?://[^\s<]+)", r'<a href="\1" target="_blank" rel="noopener">\1</a>', text)
    return text


def toggle_checkbox_in_body(body: str, index: int) -> str | None:
    """Toggle the Nth checkbox in the body. Returns updated body or None if not found."""
    count = -1
    lines = body.split("\n")
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*[-*] \[)([ xX])(\].*)$", line)
        if m:
            count += 1
            if count == index:
                new_state = " " if m.group(2).lower() == "x" else "x"
                lines[i] = m.group(1) + new_state + m.group(3)
                return "\n".join(lines)
    return None


# ── Pages ─────────────────────────────────────────────────

@router.get("/notes", response_class=HTMLResponse)
async def notes_page(request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(NoteGroup).options(selectinload(NoteGroup.notes).selectinload(Note.images))
        .order_by(NoteGroup.sort_order, NoteGroup.name))
    groups = result.scalars().all()
    return HTMLResponse(request.app.state.templates.get_template("notes.html").render(
        request=request, groups=groups))


@router.get("/notes/group/{group_id}", response_class=HTMLResponse)
async def note_group_page(request: Request, group_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(NoteGroup).options(selectinload(NoteGroup.notes).selectinload(Note.images))
        .where(NoteGroup.id == group_id))
    group = result.scalar_one_or_none()
    if not group:
        return HTMLResponse("<div class='p-8 text-center text-red-400'>Group not found</div>", status_code=404)
    return HTMLResponse(request.app.state.templates.get_template("note_group.html").render(
        request=request, group=group))


@router.get("/notes/{note_id}", response_class=HTMLResponse)
async def note_detail_page(request: Request, note_id: int, edit: int = 0,
                           db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Note).options(selectinload(Note.images), selectinload(Note.group))
        .where(Note.id == note_id))
    note = result.scalar_one_or_none()
    if not note:
        return HTMLResponse("<div class='p-8 text-center text-red-400'>Note not found</div>", status_code=404)
    return HTMLResponse(request.app.state.templates.get_template("note_detail.html").render(
        request=request, note=note, rendered_body=render_note_body(note.body),
        edit_mode=bool(edit)))


# ── Groups API ────────────────────────────────────────────

@router.post("/api/notes/groups")
async def create_group(request: Request, db: AsyncSession = Depends(get_db)):
    form = await request.form()
    name = str(form.get("name", "")).strip()
    if not name:
        raise HTTPException(422, "name required")
    g = NoteGroup(name=name, color=str(form.get("color") or "#a78bfa"),
                  icon=str(form.get("icon") or "📁"))
    db.add(g)
    await db.commit()
    return _redirect(request, "/notes")


@router.post("/api/notes/groups/{group_id}")
async def group_form_dispatch(group_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    form = await request.form()
    method = str(form.get("_method", "")).upper()
    result = await db.execute(
        select(NoteGroup).options(selectinload(NoteGroup.notes).selectinload(Note.images))
        .where(NoteGroup.id == group_id))
    group = result.scalar_one_or_none()
    if not group:
        raise HTTPException(404, "Group not found")

    if method == "DELETE":
        for note in group.notes:
            for img in note.images:
                _unlink(os.path.join(NOTES_DIR, img.filename))
                _unlink(os.path.join(THUMBS_DIR, img.thumb_filename))
        await db.delete(group)
        await db.commit()
        return _redirect(request, "/notes")

    if method == "PUT":
        if form.get("name"):
            group.name = str(form.get("name"))
        if form.get("color"):
            group.color = str(form.get("color"))
        if form.get("icon"):
            group.icon = str(form.get("icon"))
        await db.commit()
        return _redirect(request, f"/notes/group/{group.id}")

    raise HTTPException(405, "Use _method=PUT or _method=DELETE")


# ── Notes API ─────────────────────────────────────────────

@router.post("/api/notes")
async def create_note(request: Request, db: AsyncSession = Depends(get_db)):
    form = await request.form()
    group_id = int(form.get("group_id"))
    title = str(form.get("title", "")).strip()
    if not title:
        raise HTTPException(422, "title required")
    n = Note(group_id=group_id, title=title, body=str(form.get("body") or ""))
    db.add(n)
    await db.commit()
    await db.refresh(n)
    return _redirect(request, f"/notes/{n.id}")


@router.post("/api/notes/{note_id}/toggle-checkbox")
async def toggle_checkbox(note_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    note = await db.get(Note, note_id)
    if not note:
        raise HTTPException(404, "Note not found")
    if "application/json" in request.headers.get("content-type", ""):
        index = int((await request.json()).get("index", -1))
    else:
        index = int((await request.form()).get("index", -1))
    updated = toggle_checkbox_in_body(note.body or "", index)
    if updated is None:
        raise HTTPException(422, "checkbox index not found")
    note.body = updated
    note.updated_at = _now()
    await db.commit()
    return {"ok": True}


@router.post("/api/notes/{note_id}/pin")
async def toggle_pin(note_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    note = await db.get(Note, note_id)
    if not note:
        raise HTTPException(404, "Note not found")
    note.is_pinned = 0 if note.is_pinned else 1
    await db.commit()
    return _redirect(request, f"/notes/group/{note.group_id}")


@router.post("/api/notes/{note_id}")
async def note_form_dispatch(note_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    form = await request.form()
    method = str(form.get("_method", "")).upper()
    result = await db.execute(
        select(Note).options(selectinload(Note.images)).where(Note.id == note_id))
    note = result.scalar_one_or_none()
    if not note:
        raise HTTPException(404, "Note not found")

    if method == "DELETE":
        group_id = note.group_id
        for img in note.images:
            _unlink(os.path.join(NOTES_DIR, img.filename))
            _unlink(os.path.join(THUMBS_DIR, img.thumb_filename))
        await db.delete(note)
        await db.commit()
        return _redirect(request, f"/notes/group/{group_id}")

    if method == "PUT":
        if form.get("title"):
            note.title = str(form.get("title"))
        body = form.get("body")
        if body is not None:
            note.body = str(body)
        gid = form.get("group_id")
        if gid:
            note.group_id = int(gid)
        note.updated_at = _now()
        await db.commit()
        return _redirect(request, f"/notes/{note.id}")

    raise HTTPException(405, "Use _method=PUT or _method=DELETE")


# ── Note images ───────────────────────────────────────────

@router.post("/api/notes/{note_id}/images")
async def upload_note_image(note_id: int, request: Request, file: UploadFile = File(...),
                            db: AsyncSession = Depends(get_db)):
    note = await db.get(Note, note_id)
    if not note:
        raise HTTPException(404, "Note not found")
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in IMAGE_EXT:
        raise HTTPException(400, f"Image type '{ext}' not allowed")
    uid = uuid.uuid4().hex
    filename = f"{uid}{ext}"
    thumb_filename = f"{uid}_thumb.webp"
    full = os.path.join(NOTES_DIR, filename)
    with open(full, "wb") as f:
        import shutil
        shutil.copyfileobj(file.file, f)
    try:
        _make_thumbnail(full, os.path.join(THUMBS_DIR, thumb_filename))
    except Exception:
        _unlink(full)
        raise HTTPException(400, "Could not process image")
    db.add(NoteImage(note_id=note_id, filename=filename, thumb_filename=thumb_filename,
                     original_name=file.filename or filename))
    note.updated_at = _now()
    await db.commit()
    return _redirect(request, f"/notes/{note_id}")


@router.post("/api/notes/images/{image_id}/delete")
async def delete_note_image(image_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    img = await db.get(NoteImage, image_id)
    if not img:
        raise HTTPException(404, "Image not found")
    note_id = img.note_id
    _unlink(os.path.join(NOTES_DIR, img.filename))
    _unlink(os.path.join(THUMBS_DIR, img.thumb_filename))
    await db.delete(img)
    await db.commit()
    return _redirect(request, f"/notes/{note_id}")
