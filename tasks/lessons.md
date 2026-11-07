# Lessons

- Ask, don't assume: the user wants questions surfaced before decisions (2026-11-07).
- The look comes from the design team's stylesheet (midnight-mac-showcase `/compare`, "restyled native app"). Restyle via CSS only (`static/app.css`, `server/admin/admin.css`); when markup changes, add matching rules in the same style instead of plain or light-theme ones (2026-11-07).
- uv marks `.venv` contents hidden on this Mac and Python 3.13 skips hidden `.pth` files, so the editable install is invisible: pytest uses `pythonpath = ["src", ...]`, dev runs use `PYTHONPATH=src`. Frozen builds are unaffected.
- "Select a folder visually" means the native OS dialog (Finder / File Explorer, via pywebview), like Claude Code does. Reach for the platform's own picker before building a custom in-app widget (2026-11-07).
- Fetch origin before planning: teammates push straight to main (the tab split landed while a plan for it was being written) (2026-11-07).
- Test LAN features across a network change too: people hop between Wi-Fi and hotspots, and mDNS adverts and browsers are bound to the interfaces present when they start (2026-11-07).
- Test LAN features in the packaged app, not only from source: a source run inherits the terminal's Local Network permission, the `.app` needs its own. "No route to host" (errno 65) to a LAN address means macOS Local Network privacy is blocking that app; it can still reach the router, and it sees its own mDNS advert over loopback, so neither proves the LAN works (2026-11-07).
