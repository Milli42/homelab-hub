#!/usr/bin/env python3
"""
One-shot migration: home-maintenance + dog-vax SQLite DBs → homelab_hub.db,
plus file copies (task images, vaccine uploads/avatars).

Usage:
    python scripts/migrate.py \
        --maintenance-db /path/home_maintenance.db \
        --maintenance-images /path/images \
        --vax-db /path/vaccines.db \
        --vax-uploads /path/uploads \
        --target-db /data/homelab_hub.db \
        --target-images /data/images \
        --target-uploads /data/uploads

Idempotent-ish: refuses to run if the target DB already has tasks or pets.
"""
import argparse
import os
import shutil
import sqlite3
import sys

TASK_TABLES = ["categories", "tasks", "task_history", "photos"]
VAX_TABLES = ["pets", "vaccines", "weight_logs", "settings"]


def copy_table(src: sqlite3.Connection, dst: sqlite3.Connection, table: str,
               col_map: dict | None = None, extra: dict | None = None):
    src.row_factory = sqlite3.Row
    rows = src.execute(f"SELECT * FROM {table}").fetchall()
    if not rows:
        print(f"  {table}: 0 rows")
        return 0
    dst_cols = [r[1] for r in dst.execute(f"PRAGMA table_info({table})").fetchall()]
    n = 0
    for row in rows:
        data = dict(row)
        if col_map:
            for old, new in col_map.items():
                if old in data:
                    data[new] = data.pop(old)
        if extra:
            data.update(extra)
        data = {k: v for k, v in data.items() if k in dst_cols}
        cols = ", ".join(data.keys())
        ph = ", ".join("?" for _ in data)
        dst.execute(f"INSERT INTO {table} ({cols}) VALUES ({ph})", list(data.values()))
        n += 1
    print(f"  {table}: {n} rows")
    return n


def copy_files(src_dir: str, dst_dir: str, label: str):
    if not src_dir or not os.path.isdir(src_dir):
        print(f"  {label}: source dir missing, skipped ({src_dir})")
        return 0
    os.makedirs(dst_dir, exist_ok=True)
    n = 0
    for f in os.listdir(src_dir):
        full = os.path.join(src_dir, f)
        if os.path.isfile(full):
            shutil.copy2(full, os.path.join(dst_dir, f))
            n += 1
    print(f"  {label}: {n} files")
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--maintenance-db", required=True)
    ap.add_argument("--maintenance-images", default=None)
    ap.add_argument("--vax-db", required=True)
    ap.add_argument("--vax-uploads", default=None)
    ap.add_argument("--target-db", required=True)
    ap.add_argument("--target-images", required=True)
    ap.add_argument("--target-uploads", required=True)
    args = ap.parse_args()

    if not os.path.exists(args.target_db):
        sys.exit(f"Target DB {args.target_db} does not exist — start the app once to create the schema.")

    dst = sqlite3.connect(args.target_db)
    dst.execute("PRAGMA foreign_keys=OFF")

    # Safety: refuse if already migrated
    existing_tasks = dst.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    existing_pets = dst.execute("SELECT COUNT(*) FROM pets").fetchone()[0]
    if existing_tasks or existing_pets:
        sys.exit(f"Target already has data (tasks={existing_tasks}, pets={existing_pets}). Aborting.")

    # Seeded categories would collide with migrated ones — wipe them first
    dst.execute("DELETE FROM categories")

    print("── Migrating Home Maintenance ──")
    src_m = sqlite3.connect(args.maintenance_db)
    for table in TASK_TABLES:
        copy_table(src_m, dst, table)
    src_m.close()

    print("── Migrating Dog Vax ──")
    src_v = sqlite3.connect(args.vax_db)
    src_v.row_factory = sqlite3.Row

    # pets: avatar_path "uploads/avatar_1.png" → basename
    for row in src_v.execute("SELECT * FROM pets").fetchall():
        d = dict(row)
        if d.get("avatar_path"):
            d["avatar_path"] = os.path.basename(d["avatar_path"])
        dst.execute(
            "INSERT INTO pets (id, name, species, breed, birth_date, avatar_path, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (d["id"], d["name"], d.get("species", "dog"), d.get("breed"),
             d.get("birth_date"), d.get("avatar_path"), d.get("created_at")))
    print(f"  pets: {dst.execute('SELECT COUNT(*) FROM pets').fetchone()[0]} rows")

    # vaccines: file_path basename too
    for row in src_v.execute("SELECT * FROM vaccines").fetchall():
        d = dict(row)
        fp = os.path.basename(d["file_path"]) if d.get("file_path") else None
        dst.execute(
            "INSERT INTO vaccines (id, pet_id, vaccine_name, administered_date, due_date, "
            "file_path, notes, is_archived, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (d["id"], d["pet_id"], d["vaccine_name"], d["administered_date"], d["due_date"],
             fp, d.get("notes"), d.get("is_archived", 0), d.get("created_at")))
    print(f"  vaccines: {dst.execute('SELECT COUNT(*) FROM vaccines').fetchone()[0]} rows")

    copy_table(src_v, dst, "weight_logs")
    # settings: keep backup config
    for row in src_v.execute("SELECT * FROM settings").fetchall():
        dst.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                    (row["key"], row["value"]))
    print("  settings: copied")
    src_v.close()

    dst.commit()

    print("── Copying files ──")
    copy_files(args.maintenance_images, args.target_images, "task images")
    copy_files(args.vax_uploads, args.target_uploads, "vax uploads/avatars")

    dst.close()
    print("✅ Migration complete.")


if __name__ == "__main__":
    main()
