from __future__ import annotations

from typing import Protocol

import httpx

from app.core.config import get_settings
from app.schemas.editing import EditRecipe, EditingRenderResult, EditingVideoInput


class RendererError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class EditingRenderer(Protocol):
    def render(
        self,
        *,
        run_id: str,
        recipe: EditRecipe,
        videos: list[EditingVideoInput],
    ) -> EditingRenderResult: ...


class HttpEditingRenderer:
    """Adapter for the REALS Renderer service; only validated recipes reach it."""

    def __init__(self) -> None:
        settings = get_settings()
        self.url = settings.editing_renderer_url.rstrip("/")
        self.timeout = settings.editing_renderer_timeout_seconds
        self.internal_api_key = settings.effective_internal_api_key

    def render(
        self,
        *,
        run_id: str,
        recipe: EditRecipe,
        videos: list[EditingVideoInput],
    ) -> EditingRenderResult:
        if not self.url:
            raise RendererError("EDITING_RENDERER_URL is not configured.", retryable=False)
        headers = {"Content-Type": "application/json"}
        if self.internal_api_key:
            headers["X-Internal-API-Key"] = self.internal_api_key
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(
                    f"{self.url}/renders",
                    headers=headers,
                    json={
                        "run_id": run_id,
                        "recipe": recipe.model_dump(mode="json"),
                        "videos": [video.model_dump(mode="json") for video in videos],
                        "policies": {
                            "original_audio": "REMOVE",
                            "bgm": "NONE",
                            "flow_preserved": True,
                        },
                    },
                )
        except httpx.TimeoutException as exc:
            raise RendererError("Renderer request timed out.") from exc
        except httpx.HTTPError as exc:
            raise RendererError("Renderer request failed.") from exc
        if response.status_code >= 400:
            raise RendererError(
                f"Renderer returned HTTP {response.status_code}.",
                retryable=response.status_code == 429 or response.status_code >= 500,
            )
        try:
            payload = response.json()
            if payload.get("deliverable") is False:
                raise RendererError("Renderer QC did not produce a deliverable video.")
            return EditingRenderResult.model_validate(payload.get("render", payload))
        except (ValueError, TypeError) as exc:
            raise RendererError("Renderer returned an invalid result.") from exc
