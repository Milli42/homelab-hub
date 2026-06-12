# Homelab Hub

Unified self-hosted home dashboard: home maintenance tasks, project notes (with photos and checklists), and dog vaccine tracking — one app, one database, one container.

## Tabs
- **Dashboard** — unified calendar (task + vaccine due dates), needs-attention feed, pet countdowns, recent notes
- **Tasks** — recurring home maintenance (complete/skip/snooze, categories, photos)
- **Notes** — project notes in groups/folders, interactive checkboxes, photo thumbnails
- **Recipes** — recipe box by category, ingredients (tap to check off), step-by-step directions, photo thumbnails
- **Dog Vax** — vaccine records with re-vax archiving, weight chart, file attachments, backups

## Stack
FastAPI · SQLAlchemy/aiosqlite · Jinja2 + HTMX · Tailwind · Pillow (thumbnails) · Docker (GHCR)

## Deploy
See `docker-compose.yml` — port 8052, volume `/mnt/user/appdata/homelab-hub:/data`.

## Migration from the old apps
`scripts/migrate.py` imports home-maintenance-tracker and dog-vax-tracker SQLite DBs + files. Run once against a fresh DB.
