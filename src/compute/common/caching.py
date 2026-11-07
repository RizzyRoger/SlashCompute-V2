"""How browsers cache the UI's own files (the app's ``static/`` and the admin page).

``Cache-Control: no-cache`` means "keep a copy, but check with the server before using it". Without it,
a browser guesses how long a file stays fresh, so after an update it can run last version's JS
with this version's HTML and CSS. With it, every load asks first; unchanged files come back as a
cheap ``304 Not Modified`` where the server answers conditional requests (``StaticFiles`` does).
"""

from __future__ import annotations

from starlette.responses import Response
from starlette.staticfiles import StaticFiles

REVALIDATE = {"Cache-Control": "no-cache"}


class RevalidatedStaticFiles(StaticFiles):
    """``StaticFiles`` whose files, and the 304s that stand in for them, say ``no-cache``."""

    def file_response(self, *args, **kwargs) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers.update(REVALIDATE)
        return response
