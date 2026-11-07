"""Local app shell on 127.0.0.1:8766: serves the UI, controls the processes, proxies the main server."""

from __future__ import annotations

import os
from dataclasses import asdict
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from compute.app.attachments import AttachmentError, extract, max_bytes_for
from compute.app.code.harness import CodeManager
from compute.app.code.routes import code_router
from compute.app.controller import Controller, ControllerError
from compute.app.folders import FolderError, browse
from compute.app.resources import static_dir
from compute.common.caching import REVALIDATE, RevalidatedStaticFiles

SHELL_HOST = "127.0.0.1"
SHELL_PORT = int(os.environ.get("COMPUTE_SHELL_PORT", "8766"))
SHELL_GENERATION = 11
# Forming a pipeline (loading weights over RPC) can take minutes before the first token.
STREAM_TIMEOUT = httpx.Timeout(None, connect=5.0)
PROXY_TIMEOUT = httpx.Timeout(60.0, connect=5.0)
PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]
PASS_REQUEST_HEADERS = ("content-type", "accept", "cookie")


def local_origins(port: int) -> set[str]:
    return {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}


def local_hosts(port: int) -> set[str]:
    return {f"127.0.0.1:{port}", f"localhost:{port}"}


def bad_path(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    return ".." in parts or not path.strip("/")


def forward_headers(request: Request, session_token: str) -> dict[str, str]:
    """Auth for the main server: the browser's own header wins, else the saved session token."""
    headers = {k: request.headers[k] for k in PASS_REQUEST_HEADERS if request.headers.get(k)}
    auth = request.headers.get("authorization")
    if auth:
        headers["authorization"] = auth
    elif session_token:
        headers["authorization"] = f"Bearer {session_token}"
    return headers


def relay_response(r: httpx.Response) -> Response:
    resp = Response(content=r.content, status_code=r.status_code,
                    media_type=r.headers.get("content-type", "application/json"))
    for cookie in r.headers.get_list("set-cookie"):
        resp.headers.append("set-cookie", cookie)
    return resp


def error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"detail": message}, status_code=status)


def create_shell(controller: Optional[Controller] = None,
                 client: Optional[httpx.AsyncClient] = None,
                 port: int = SHELL_PORT) -> FastAPI:
    ctl = controller or Controller()
    if controller is None:
        ctl.lan_servers()   # the real app: start browsing mDNS now, so the first status poll can join
    http = client or httpx.AsyncClient(timeout=PROXY_TIMEOUT)
    app = FastAPI(title="/compute app shell")
    app.state.controller = ctl
    origins, hosts = local_origins(port), local_hosts(port)

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        """Refuse other websites (CSRF) and DNS-rebinding hosts: this API starts processes."""
        origin = request.headers.get("origin")
        if origin and origin not in origins:
            return error(403, "Cross-origin request refused.")
        if request.headers.get("host") not in hosts:
            return error(403, "Unknown host.")
        return await call_next(request)

    def server_base() -> str:
        url = ctl.load_settings().server_url
        if not url:
            raise HTTPException(503, "Not connected to a main server. Find one or enter its address.")
        return url

    # ------------------------------------------------------------------- UI

    @app.get("/")
    def index():
        path = static_dir() / "index.html"
        if not path.is_file():
            raise HTTPException(500, "UI files missing")
        return FileResponse(path, headers=REVALIDATE)

    @app.get("/api/shell")
    def shell_info():
        return {"ok": True, "app": "compute", "generation": SHELL_GENERATION}

    # ------------------------------------------------------- settings/status

    @app.get("/api/settings")
    def get_settings():
        return asdict(ctl.load_settings())

    @app.post("/api/settings")
    def post_settings(body: dict):
        return asdict(ctl.update_settings(body))

    @app.get("/api/status")
    def status():
        """The UI polls this every few seconds while the window is open: join this network's pool too."""
        ctl.follow_pool()
        return ctl.status()

    @app.post("/api/discover")
    def discover():
        """Join this network's pool (turns auto-join back on after a typed address)."""
        url = ctl.discover()
        if not url:
            raise HTTPException(404, "No main server found on this network.")
        return {"url": url}

    # -------------------------------------------------------------- processes

    def run_action(action, *args):
        try:
            return action(*args)
        except ControllerError as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/api/server/start")
    def server_start(body: dict):
        return run_action(ctl.start_server, str(body.get("email", "")), str(body.get("password", "")))

    @app.post("/api/server/stop")
    def server_stop():
        return run_action(ctl.stop_server)

    @app.post("/api/node/start")
    def node_start():
        return run_action(ctl.start_node)

    @app.post("/api/node/stop")
    def node_stop():
        return run_action(ctl.stop_node)

    # ----------------------------------------------------------------- proxy

    @app.api_route("/api/coord/{path:path}", methods=PROXY_METHODS)
    async def coord(path: str, request: Request):
        if bad_path(path):
            raise HTTPException(404, "Not proxied.")
        url = f"{server_base()}/{path.lstrip('/')}"
        headers = forward_headers(request, ctl.load_settings().session_token)
        try:
            r = await http.request(request.method, url, params=list(request.query_params.multi_items()),
                                   content=await request.body(), headers=headers)
        except httpx.HTTPError as e:
            raise HTTPException(502, f"Main server unreachable at {server_base()}: {e}") from e
        return relay_response(r)

    @app.post("/api/chat")
    async def chat(request: Request):
        """SSE passthrough of /v1/chat/completions, `queued` events included."""
        body = await request.json()
        body["stream"] = True
        headers = forward_headers(request, ctl.load_settings().session_token)
        headers["content-type"] = "application/json"
        req = http.build_request("POST", f"{server_base()}/v1/chat/completions", json=body,
                                 headers=headers, timeout=STREAM_TIMEOUT)
        try:
            r = await http.send(req, stream=True)
        except httpx.HTTPError as e:
            raise HTTPException(502, f"Main server unreachable: {e}") from e
        if r.status_code >= 400:
            await r.aread()
            await r.aclose()
            return relay_response(r)
        return StreamingResponse(r.aiter_bytes(), status_code=r.status_code,
                                 media_type=r.headers.get("content-type", "text/event-stream"),
                                 headers={"cache-control": "no-cache"},
                                 background=BackgroundTask(r.aclose))

    # ------------------------------------------------------------ attachments

    @app.post("/api/attach")
    async def attach(file: UploadFile):
        name = file.filename or "file"
        data = await file.read(max_bytes_for(name) + 1)
        try:
            return extract(name, data)
        except AttachmentError as e:
            return error(e.status, e.message)

    # ---------------------------------------------------------------- folders

    @app.get("/api/folders")
    def folders(path: str = "", hidden: bool = False):
        """The folder picker: one folder's sub-folders on this computer (names only)."""
        try:
            return browse(path, hidden)
        except FolderError as e:
            return error(e.status, e.message)

    # ------------------------------------------------------------- code tabs

    app.state.code = CodeManager(ctl.home, ctl.load_settings, http)
    app.include_router(code_router(app.state.code))

    app.mount("/static", RevalidatedStaticFiles(directory=static_dir(), check_dir=False), name="static")
    return app
