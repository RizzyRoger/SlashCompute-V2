# Lessons

- Ask, don't assume: the user wants questions surfaced before decisions (2026-11-07).
- The look comes from the design team's stylesheet (midnight-mac-showcase `/compare`, "restyled native app"). Restyle via CSS only (`static/app.css`, `server/admin/admin.css`); when markup changes, add matching rules in the same style instead of plain or light-theme ones (2026-11-07).
- uv marks `.venv` contents hidden on this Mac and Python 3.13 skips hidden `.pth` files, so the editable install is invisible: pytest uses `pythonpath = ["src", ...]`, dev runs use `PYTHONPATH=src`. Frozen builds are unaffected.
- "Select a folder visually" means the native OS dialog (Finder / File Explorer, via pywebview), like Claude Code does. Reach for the platform's own picker before building a custom in-app widget (2026-11-07).
