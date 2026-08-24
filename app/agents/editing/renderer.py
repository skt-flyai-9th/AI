from __future__ import annotations

from typing import Any, Protocol

import httpx

from app.core.config import get_settings
from app.agents.editing.reals import RealsRecipeAdapter
from app.agents.editing.types import VideoContext
from app.schemas.editing import EditRecipe, EditingRenderResult, EditingVideoInput


class RendererError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "RENDERER_ERROR",
        retryable: bool = True,
        details: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.details = details or []


class EditingRenderer(Protocol):
    def render(
        self,
        *,
        run_id: str,
        recipe: EditRecipe,
        videos: list[EditingVideoInput],
        video_contexts: list[VideoContext],
        video_editing_db: dict[str, Any],
    ) -> EditingRenderResult: ...


class HttpEditingRenderer:
    """Adapter for the REALS Renderer service; only validated recipes reach it."""

    def __init__(self, adapter: RealsRecipeAdapter | None = None) -> None:
        settings = get_settings()
        self.url = settings.editing_renderer_url.rstrip("/")
        self.timeout = settings.editing_renderer_timeout_seconds
        self.internal_api_key = settings.effective_internal_api_key
        self.adapter = adapter or RealsRecipeAdapter()

    def render(
        self,
        *,
        run_id: str,
        recipe: EditRecipe,
        videos: list[EditingVideoInput],
        video_contexts: list[VideoContext],
        video_editing_db: dict[str, Any],
    ) -> EditingRenderResult:
        if not self.url:
            raise RendererError(
                "EDITING_RENDERER_URL is not configured.",
                code="RENDERER_NOT_CONFIGURED",
                retryable=False,
            )
        try:
            render_request = self.adapter.build_request(
                run_id=run_id,
                recipe=recipe,
                videos=videos,
                video_contexts=video_contexts,
                video_editing_db=video_editing_db,
            )
        except ValueError as exc:
            raise RendererError(
                f"Could not adapt recipe to the REALS contract: {exc}",
                code="REALS_ADAPTER_INVALID_INPUT",
                retryable=False,
            ) from exc
        headers = {"Content-Type": "application/json"}
        if self.internal_api_key:
            headers["X-Internal-API-Key"] = self.internal_api_key
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(
                    f"{self.url}/renders",
                    headers=headers,
                    json=render_request.model_dump(mode="json"),
                )
        except httpx.TimeoutException as exc:
            raise RendererError(
                "Renderer request timed out.", code="RENDERER_TIMEOUT"
            ) from exc
        except httpx.HTTPError as exc:
            raise RendererError(
                "Renderer request failed.", code="RENDERER_NETWORK_ERROR"
            ) from exc
        if response.status_code >= 400:
            _raise_renderer_http_error(response)
        try:
            payload = response.json()
            if payload.get("deliverable") is False:
                details = _renderer_details(payload)
                raise RendererError(
                    str(payload.get("error") or "Renderer QC did not produce a deliverable video."),
                    code=str(payload.get("code") or "REALS_QC_NOT_DELIVERABLE"),
                    retryable=bool(payload.get("retryable", False)),
                    details=details,
                )
            return EditingRenderResult.model_validate(payload.get("render", payload))
        except RendererError:
            raise
        except (ValueError, TypeError) as exc:
            raise RendererError(
                "Renderer returned an invalid result.",
                code="RENDERER_RESPONSE_INVALID",
                retryable=False,
            ) from exc


def _raise_renderer_http_error(response: httpx.Response) -> None:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    raise RendererError(
        str(payload.get("error") or payload.get("message") or f"Renderer returned HTTP {response.status_code}."),
        code=str(payload.get("code") or f"RENDERER_HTTP_{response.status_code}"),
        retryable=response.status_code == 429 or response.status_code >= 500,
        details=_renderer_details(payload),
    )


def _renderer_details(payload: dict[str, Any]) -> list[dict[str, Any]]:
    value = payload.get("validation_errors") or payload.get("errors")
    if not isinstance(value, list):
        qc = payload.get("qc")
        value = qc.get("checks") if isinstance(qc, dict) else []
    return [item for item in value if isinstance(item, dict)]
