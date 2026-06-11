"""
Dog Vax module routes — pets, vaccines, weights, backups.
Ported from the standalone dog-vax-tracker into Homelab Hub (async SQLAlchemy).
Form-encoded posts redirect back to /dogvax; JSON API preserved for widgets/agent.
"""
import io
import os
import shutil
import tempfile
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from sqlalchemy import select, func, delete, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db, DATABASE_PATH
from app.models import Pet, Vaccine, WeightLog, Setting

router = APIRouter(tags=["dogvax"])

UPLOADS_DIR = os.environ.get("UPLOADS_DIR", "data/uploads")
AUTO_BACKUP_DIR = Path(os.environ.get("AUTO_BACKUP_DIR", "/user/backups/animalvaccines"))
ALLOWED_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

os.makedirs(UPLOADS_DIR, exist_ok=True)


def _redirect(request: Request, url: str):
    if request.headers.get("HX-Request"):
        return HTMLResponse(status_code=200, headers={"HX-Redirect": url})
    return RedirectResponse(url=url, status_code=303)


async def _get_pet_or_404(db: AsyncSession, pet_id: int) -> Pet:
    result = await db.execute(
        select(Pet).options(selectinload(Pet.vaccines), selectinload(Pet.weights))
        .where(Pet.id == pet_id))
    pet = result.scalar_one_or_none()
    if not pet:
        raise HTTPException(404, "Pet not found")
    return pet


async def _get_setting(db: AsyncSession, key: str, default: str = "") -> str:
    row = await db.scalar(select(Setting.value).where(Setting.key == key))
    return row if row is not None else default


async def _set_setting(db: AsyncSession, key: str, value: str):
    existing = await db.get(Setting, key)
    if existing:
        existing.value = value
    else:
        db.add(Setting(key=key, value=value))
    await db.commit()


# ── Page ──────────────────────────────────────────────────

@router.get("/dogvax", response_class=HTMLResponse)
async def dogvax_page(request: Request, pet: int | None = None, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Pet).options(selectinload(Pet.vaccines), selectinload(Pet.weights))
        .order_by(Pet.name))
    pets = result.scalars().all()

    selected = None
    if pets:
        selected = next((p for p in pets if p.id == pet), pets[0])

    current_vaccines, archived_vaccines, weights, weights_chart = [], [], [], []
    if selected:
        current_vaccines = sorted([v for v in selected.vaccines if not v.is_archived],
                                  key=lambda v: v.due_date)
        archived_vaccines = sorted([v for v in selected.vaccines if v.is_archived],
                                   key=lambda v: v.created_at, reverse=True)
        weights = sorted(selected.weights, key=lambda w: w.date, reverse=True)
        weights_chart = [{"date": w.date, "weight": w.weight} for w in reversed(weights)]

    return HTMLResponse(request.app.state.templates.get_template("dogvax.html").render(
        request=request, pets=pets, selected=selected,
        current_vaccines=current_vaccines, archived_vaccines=archived_vaccines,
        weights=weights, weights_chart=weights_chart, today=date.today(),
    ))


# ── Pets API ──────────────────────────────────────────────

@router.get("/api/pets")
async def api_list_pets(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Pet).order_by(Pet.name))
    return [{"id": p.id, "name": p.name, "species": p.species, "breed": p.breed,
             "birth_date": p.birth_date, "avatar_path": p.avatar_path} for p in result.scalars()]


@router.post("/api/pets")
async def api_create_pet(request: Request, name: str = Form(...), species: str = Form("dog"),
                         breed: str = Form(""), birth_date: str = Form(""),
                         db: AsyncSession = Depends(get_db)):
    p = Pet(name=name, species=species, breed=breed or None, birth_date=birth_date or None)
    db.add(p)
    await db.commit()
    return _redirect(request, f"/dogvax?pet={p.id}")


@router.post("/api/pets/{pet_id}")
async def api_pet_form_dispatch(pet_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Form dispatch: _method=PUT (update) or _method=DELETE."""
    form = await request.form()
    method = str(form.get("_method", "")).upper()
    pet = await _get_pet_or_404(db, pet_id)

    if method == "DELETE":
        # remove avatar + vaccine files from disk
        if pet.avatar_path:
            _unlink(os.path.join(UPLOADS_DIR, os.path.basename(pet.avatar_path)))
        for v in pet.vaccines:
            if v.file_path:
                _unlink(os.path.join(UPLOADS_DIR, os.path.basename(v.file_path)))
        await db.delete(pet)
        await db.commit()
        return _redirect(request, "/dogvax")

    if method == "PUT":
        for field in ("name", "species", "breed", "birth_date"):
            val = form.get(field)
            if val is not None:
                setattr(pet, field, str(val) or None)
        await db.commit()
        return _redirect(request, f"/dogvax?pet={pet.id}")

    raise HTTPException(405, "Use _method=PUT or _method=DELETE")


def _unlink(path: str):
    try:
        os.remove(path)
    except OSError:
        pass


# ── Avatar ────────────────────────────────────────────────

@router.get("/api/pets/{pet_id}/avatar")
async def api_get_avatar(pet_id: int, db: AsyncSession = Depends(get_db)):
    pet = await db.get(Pet, pet_id)
    if not pet or not pet.avatar_path:
        raise HTTPException(404, "No avatar")
    full = os.path.join(UPLOADS_DIR, os.path.basename(pet.avatar_path))
    if not os.path.exists(full):
        raise HTTPException(404, "Avatar file missing")
    return FileResponse(full)


@router.post("/api/pets/{pet_id}/avatar")
async def api_upload_avatar(pet_id: int, request: Request, file: UploadFile = File(...),
                            db: AsyncSession = Depends(get_db)):
    pet = await db.get(Pet, pet_id)
    if not pet:
        raise HTTPException(404, "Pet not found")
    ext = Path(file.filename).suffix.lower()
    if ext not in IMAGE_EXT:
        raise HTTPException(400, f"Image type '{ext}' not allowed")
    safe_name = f"avatar_{pet_id}{ext}"
    dest = os.path.join(UPLOADS_DIR, safe_name)
    old = pet.avatar_path
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
    pet.avatar_path = safe_name
    await db.commit()
    if old and os.path.basename(old) != safe_name:
        _unlink(os.path.join(UPLOADS_DIR, os.path.basename(old)))
    return _redirect(request, f"/dogvax?pet={pet_id}")


# ── Weights ───────────────────────────────────────────────

@router.post("/api/pets/{pet_id}/weights")
async def api_add_weight(pet_id: int, request: Request, weight: float = Form(...),
                         date_: str = Form(..., alias="date"), notes: str = Form(""),
                         db: AsyncSession = Depends(get_db)):
    if not await db.get(Pet, pet_id):
        raise HTTPException(404, "Pet not found")
    db.add(WeightLog(pet_id=pet_id, weight=weight, date=date_, notes=notes or None))
    await db.commit()
    return _redirect(request, f"/dogvax?pet={pet_id}")


@router.post("/api/weights/{weight_id}/delete")
async def api_delete_weight(weight_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    w = await db.get(WeightLog, weight_id)
    if not w:
        raise HTTPException(404, "Weight not found")
    pet_id = w.pet_id
    await db.delete(w)
    await db.commit()
    return _redirect(request, f"/dogvax?pet={pet_id}")


# ── Vaccines ──────────────────────────────────────────────

@router.post("/api/vaccines")
async def api_create_vaccine(request: Request, pet_id: int = Form(...),
                             vaccine_name: str = Form(...), administered_date: str = Form(...),
                             due_date: str = Form(...), notes: str = Form(""),
                             file: Optional[UploadFile] = File(None),
                             db: AsyncSession = Depends(get_db)):
    file_path = None
    if file and file.filename:
        ext = Path(file.filename).suffix.lower()
        if ext not in ALLOWED_EXT:
            raise HTTPException(400, f"File type '{ext}' not allowed")
        safe_name = f"{date.today().isoformat()}_{pet_id}_{file.filename}"
        with open(os.path.join(UPLOADS_DIR, safe_name), "wb") as f:
            shutil.copyfileobj(file.file, f)
        file_path = safe_name

    # Re-vax: archive existing current record with same pet+name
    await db.execute(update(Vaccine).where(
        Vaccine.pet_id == pet_id, Vaccine.vaccine_name == vaccine_name,
        Vaccine.is_archived == 0).values(is_archived=1))
    db.add(Vaccine(pet_id=pet_id, vaccine_name=vaccine_name,
                   administered_date=administered_date, due_date=due_date,
                   file_path=file_path, notes=notes or None))
    await db.commit()
    return _redirect(request, f"/dogvax?pet={pet_id}")


# Static routes BEFORE /api/vaccines/{id} routes
@router.get("/api/vaccines/alerts")
async def api_get_alerts(db: AsyncSession = Depends(get_db)):
    today = date.today()
    cutoff = (today + timedelta(days=30)).isoformat()
    result = await db.execute(
        select(Vaccine).options(selectinload(Vaccine.pet))
        .where(Vaccine.is_archived == 0, Vaccine.due_date <= cutoff)
        .order_by(Vaccine.due_date))
    return [{"pet_name": v.pet.name, "vaccine_name": v.vaccine_name, "due_date": v.due_date,
             "status": "overdue" if v.due_date < today.isoformat() else "expiring_soon"}
            for v in result.scalars()]


@router.get("/api/vaccines/dashboard")
async def api_vax_dashboard(db: AsyncSession = Depends(get_db)):
    """Per-pet countdown for Homepage customapi widget (flat dict)."""
    today = date.today()
    result = await db.execute(
        select(Pet).options(selectinload(Pet.vaccines)).order_by(Pet.name))
    out = {}
    for pet in result.scalars():
        key = pet.name.replace(" ", "_")
        active = [v for v in pet.vaccines if not v.is_archived]
        future = sorted([v for v in active if v.due_date >= today.isoformat()], key=lambda v: v.due_date)
        pick = future[0] if future else (
            sorted([v for v in active if v.due_date < today.isoformat()],
                   key=lambda v: v.due_date, reverse=True) or [None])[0]
        if pick is None:
            out[f"{key}_days"], out[f"{key}_vaccine"] = None, "None"
        else:
            out[f"{key}_days"] = (date.fromisoformat(pick.due_date) - today).days
            out[f"{key}_vaccine"] = pick.vaccine_name
    return out


@router.get("/api/vaccines/{vaccine_id}/file")
async def api_get_vaccine_file(vaccine_id: int, db: AsyncSession = Depends(get_db)):
    v = await db.get(Vaccine, vaccine_id)
    if not v or not v.file_path:
        raise HTTPException(404, "No file")
    full = os.path.join(UPLOADS_DIR, os.path.basename(v.file_path))
    if not os.path.exists(full):
        raise HTTPException(404, "File missing on disk")
    return FileResponse(full)


@router.post("/api/vaccines/{vaccine_id}/attach")
async def api_attach_file(vaccine_id: int, request: Request, file: UploadFile = File(...),
                          db: AsyncSession = Depends(get_db)):
    v = await db.get(Vaccine, vaccine_id)
    if not v:
        raise HTTPException(404, "Vaccine not found")
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, f"File type '{ext}' not allowed")
    safe_name = f"{date.today().isoformat()}_{vaccine_id}_{file.filename}"
    with open(os.path.join(UPLOADS_DIR, safe_name), "wb") as f:
        shutil.copyfileobj(file.file, f)
    old = v.file_path
    v.file_path = safe_name
    await db.commit()
    if old:
        _unlink(os.path.join(UPLOADS_DIR, os.path.basename(old)))
    return _redirect(request, f"/dogvax?pet={v.pet_id}")


@router.post("/api/vaccines/{vaccine_id}/delete")
async def api_delete_vaccine(vaccine_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    v = await db.get(Vaccine, vaccine_id)
    if not v:
        raise HTTPException(404, "Vaccine not found")
    pet_id = v.pet_id
    if v.file_path:
        _unlink(os.path.join(UPLOADS_DIR, os.path.basename(v.file_path)))
    await db.delete(v)
    await db.commit()
    return _redirect(request, f"/dogvax?pet={pet_id}")


# ── Backup & Restore ─────────────────────────────────────

def _build_backup_zip(images_dir: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if os.path.exists(DATABASE_PATH):
            zf.write(DATABASE_PATH, "homelab_hub.db")
        for dirname, arcname in ((UPLOADS_DIR, "uploads"), (images_dir, "images")):
            if os.path.isdir(dirname):
                for f in sorted(os.listdir(dirname)):
                    full = os.path.join(dirname, f)
                    if os.path.isfile(full):
                        zf.write(full, f"{arcname}/{f}")
    return buf.getvalue()


@router.get("/api/backup")
async def api_create_backup():
    images_dir = os.environ.get("IMAGES_DIR", "data/images")
    data = _build_backup_zip(images_dir)
    filename = f"homelab-hub-backup-{date.today().isoformat()}.zip"
    return Response(content=data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.post("/api/backup/restore")
async def api_restore_backup(request: Request, file: UploadFile = File(...)):
    """Restore from a backup zip — replaces DB + uploads + images."""
    content = await file.read()
    images_dir = os.environ.get("IMAGES_DIR", "data/images")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            zf.extractall(tmp_path)
        src_db = tmp_path / "homelab_hub.db"
        if not src_db.exists():
            raise HTTPException(400, "Backup is missing homelab_hub.db")
        from app.database import engine, init_db
        await engine.dispose()
        # drop stale WAL/SHM so the restored DB is read cleanly
        for suffix in ("-wal", "-shm"):
            _unlink(DATABASE_PATH + suffix)
        shutil.copy2(str(src_db), DATABASE_PATH)
        for sub, dest in (("uploads", UPLOADS_DIR), ("images", images_dir)):
            src = tmp_path / sub
            if src.exists():
                os.makedirs(dest, exist_ok=True)
                for f in src.iterdir():
                    if f.is_file():
                        shutil.copy2(str(f), os.path.join(dest, f.name))
        await init_db()
    return _redirect(request, "/dogvax")


@router.get("/api/backup/settings")
async def api_get_backup_settings(db: AsyncSession = Depends(get_db)):
    return {
        "enabled": await _get_setting(db, "auto_backup_enabled", "false") == "true",
        "frequency": await _get_setting(db, "auto_backup_frequency", "1d"),
        "last_backup": await _get_setting(db, "auto_backup_last", ""),
    }


FREQUENCY_SECONDS = {"12h": 43200, "1d": 86400, "7d": 604800, "30d": 2592000}


@router.post("/api/backup/settings")
async def api_save_backup_settings(request: Request, enabled: str = Form("false"),
                                   frequency: str = Form("1d"),
                                   db: AsyncSession = Depends(get_db)):
    if frequency not in FREQUENCY_SECONDS:
        raise HTTPException(400, "Invalid frequency")
    await _set_setting(db, "auto_backup_enabled", "true" if enabled in ("true", "1", "on") else "false")
    await _set_setting(db, "auto_backup_frequency", frequency)
    return _redirect(request, "/dogvax")


@router.post("/api/backup/auto")
async def api_run_auto_backup(db: AsyncSession = Depends(get_db)):
    if await _get_setting(db, "auto_backup_enabled", "false") != "true":
        return {"ok": True, "backup_created": False, "reason": "auto_backup_disabled"}
    frequency = await _get_setting(db, "auto_backup_frequency", "1d")
    interval = FREQUENCY_SECONDS.get(frequency, 86400)
    last_str = await _get_setting(db, "auto_backup_last", "")
    if last_str:
        try:
            elapsed = (datetime.utcnow() - datetime.fromisoformat(last_str)).total_seconds()
            if elapsed < interval:
                return {"ok": True, "backup_created": False, "reason": "not_due_yet",
                        "last_backup": last_str,
                        "next_backup_after_seconds": int(interval - elapsed)}
        except (ValueError, TypeError):
            pass
    AUTO_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    images_dir = os.environ.get("IMAGES_DIR", "data/images")
    data = _build_backup_zip(images_dir)
    now = datetime.utcnow()
    filename = f"homelab-hub-auto-{now.strftime('%Y-%m-%dT%H%M%SZ')}.zip"
    dest = AUTO_BACKUP_DIR / filename
    dest.write_bytes(data)
    existing = sorted(AUTO_BACKUP_DIR.glob("homelab-hub-auto-*.zip"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    for old in existing[10:]:
        try:
            old.unlink()
        except OSError:
            pass
    await _set_setting(db, "auto_backup_last", now.isoformat())
    return {"ok": True, "backup_created": True, "filename": filename, "timestamp": now.isoformat()}


# ── Health ────────────────────────────────────────────────

@router.get("/api/health")
async def health():
    return {"status": "ok"}
