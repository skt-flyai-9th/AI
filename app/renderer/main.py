from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from app.agents.editing.reals import RealsRenderJobRequest
from app.core.config import get_settings
from app.core.security import require_internal_api_key, require_renderer_file_access
from app.renderer.service import (
    RendererServiceError,
    get_renderer_service,
)

settings = get_settings()
output_dir = settings.renderer_output_dir
if not output_dir.is_absolute():
    output_dir = (Path.cwd() / output_dir).resolve()
output_dir.mkdir(parents=True, exist_ok=True)

app = FastAPI(
    title="REALS Renderer Service",
    version="1.0.0",
    description="Remote-asset facade for the bundled REALS video edit engine.",
)
@app.exception_handler(RendererServiceError)
async def renderer_error_handler(
    _: Request,
    exc: RendererServiceError,
) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.payload())


@app.get("/health/live")
def live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
def ready() -> JSONResponse:
    status = get_renderer_service().ready()
    return JSONResponse(status_code=200 if status["ready"] else 503, content=status)


@app.post("/renders", dependencies=[Depends(require_internal_api_key)])
def render(request: RealsRenderJobRequest) -> dict:
    return get_renderer_service().render(request)


@app.get("/files/{filename:path}", dependencies=[Depends(require_renderer_file_access)])
def rendered_file(filename: str) -> FileResponse:
    target = (output_dir / filename).resolve()
    if target.parent != output_dir or not target.is_file():
        raise HTTPException(status_code=404, detail="Rendered file not found")
    return FileResponse(target, media_type="video/mp4", filename=target.name)
