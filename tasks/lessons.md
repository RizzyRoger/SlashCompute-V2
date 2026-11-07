# Lessons

- Ask, don't assume: the user wants questions surfaced before decisions (2026-11-07).
- Frontend is owned by other people this round: keep UI barebones, put effort into backend.
- uv marks `.venv` contents hidden on this Mac and Python 3.13 skips hidden `.pth` files, so the editable install is invisible: pytest uses `pythonpath = ["src", ...]`, dev runs use `PYTHONPATH=src`. Frozen builds are unaffected.
- "Select a folder visually" means the native OS dialog (Finder / File Explorer, via pywebview), like Claude Code does. Reach for the platform's own picker before building a custom in-app widget (2026-11-07).
