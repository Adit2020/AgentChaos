"""Local HTTP access to artifacts and explicitly acknowledged trusted jobs."""

import os
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import RequestResponseEndpoint

from agentchaos.artifacts import (
    ArtifactFormatError,
    ArtifactNotFoundError,
    ArtifactReaderError,
    ArtifactSecurityError,
    ArtifactTypeMismatchError,
    ExperimentArtifact,
    ExperimentManifest,
    MultiRunArtifact,
    MultiRunManifest,
    UnsupportedArtifactError,
    discover_exports,
    discover_multi_run_exports,
    load_export,
    load_multi_run_export,
)
from agentchaos.artifacts.writer import DEFAULT_ARTIFACT_ROOT
from agentchaos.jobs import (
    JobManager,
    JobRequest,
    JobServiceError,
    JobSettings,
    JobStatus,
)
from agentchaos.scenarios import Scenario, ScenarioMetadata, registered_scenarios


class ExportSummary(BaseModel):
    """Manifest metadata for the export list; traces are loaded on detail only."""

    export_id: str
    artifact_type: Literal["experiment", "multi_run"]
    exported_at: AwareDatetime
    manifest: ExperimentManifest | MultiRunManifest


class ExportListResponse(BaseModel):
    exports: list[ExportSummary]


class ExportDetailResponse(BaseModel):
    export_id: str
    artifact_type: Literal["experiment", "multi_run"]
    artifact: ExperimentArtifact | MultiRunArtifact


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ScenarioListResponse(BaseModel):
    scenarios: list[ScenarioMetadata]


class ErrorInfo(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorInfo


class _AmbiguousExportError(ArtifactReaderError):
    """More than one artifact reader accepted the same export ID."""


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content=ErrorResponse(error=ErrorInfo(code=code, message=message)).model_dump(),
    )


def create_app(
    artifact_root: Path | str | None = None,
    *,
    scenarios: Sequence[Scenario] | None = None,
    job_settings: JobSettings | None = None,
) -> FastAPI:
    """Create a local API with server-owned scenarios and bounded job settings.

    ``artifact_root`` takes precedence over ``AGENTCHAOS_ARTIFACT_ROOT``. The
    default is ``artifacts/`` relative to the server's starting directory.
    """

    root = Path(
        artifact_root
        if artifact_root is not None
        else os.environ.get("AGENTCHAOS_ARTIFACT_ROOT") or DEFAULT_ARTIFACT_ROOT
    ).resolve()
    jobs = JobManager(
        root,
        registered_scenarios() if scenarios is None else scenarios,
        job_settings or JobSettings(),
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        jobs.start()
        try:
            yield
        finally:
            await jobs.close()

    app = FastAPI(
        title="AgentChaos Studio API",
        version="0.0.1",
        description=(
            "Local artifact inspection and explicitly acknowledged execution of "
            "registered scenarios. Jobs are in memory; authentication and "
            "further isolation are required before public deployment."
        ),
        lifespan=lifespan,
    )
    app.state.jobs = jobs

    @app.middleware("http")
    async def protect_job_submission(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        path = request.scope["path"].removeprefix(request.scope.get("root_path", ""))
        if request.method == "POST" and path == "/api/jobs":
            if request.url.hostname not in {"127.0.0.1", "localhost", "::1"}:
                return _error(400, "invalid_job_host", "Use a loopback host for jobs.")
            # Check before body parsing, including requests without a JSON
            # content type that might bypass a browser's CORS preflight.
            origin = request.headers.get("origin")
            if origin is not None and origin != str(request.base_url).rstrip("/"):
                return _error(403, "origin_not_allowed", "Origin is not allowed.")
            media_type = request.headers.get("content-type", "").split(";", 1)[0]
            if media_type.strip().lower() != "application/json":
                return _error(415, "json_required", "Use application/json.")
        return await call_next(request)

    @app.exception_handler(JobServiceError)
    async def job_error_handler(
        _request: Request, exc: JobServiceError
    ) -> JSONResponse:
        return _error(exc.status, exc.code, exc.message)

    @app.exception_handler(ArtifactReaderError)
    async def artifact_error_handler(
        _request: Request, exc: ArtifactReaderError
    ) -> JSONResponse:
        if isinstance(exc, _AmbiguousExportError):
            return _error(409, "ambiguous_export", "Export ID is ambiguous.")
        if isinstance(exc, ArtifactNotFoundError):
            return _error(404, "export_not_found", "Export or required file not found.")
        if isinstance(exc, ArtifactSecurityError):
            return _error(
                400, "unsafe_artifact_path", "Invalid or unsafe artifact path."
            )
        if isinstance(exc, UnsupportedArtifactError):
            return _error(
                422, "unsupported_artifact", "Artifact format is unsupported."
            )
        if isinstance(exc, ArtifactFormatError):
            return _error(422, "invalid_artifact", "Artifact data is invalid.")
        return _error(500, "artifact_read_error", "Artifact could not be read.")

    @app.exception_handler(RequestValidationError)
    async def request_error_handler(
        _request: Request, _exc: RequestValidationError
    ) -> JSONResponse:
        return _error(422, "invalid_request", "Request parameters are invalid.")

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(
        _request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        if exc.status_code == 404:
            return _error(404, "not_found", "Resource not found.")
        if exc.status_code == 405:
            return _error(405, "method_not_allowed", "Method not allowed.")
        return _error(exc.status_code, "http_error", "Request could not be completed.")

    @app.get("/api/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse()

    @app.get("/api/scenarios", response_model=ScenarioListResponse)
    async def list_scenarios() -> ScenarioListResponse:
        return ScenarioListResponse(
            scenarios=[jobs.scenarios[key].metadata for key in sorted(jobs.scenarios)]
        )

    @app.post(
        "/api/jobs",
        status_code=202,
        response_model=JobStatus,
        responses={
            status: {"model": ErrorResponse}
            for status in (400, 403, 415, 422, 429, 503)
        },
    )
    async def submit_job(payload: JobRequest) -> JobStatus:
        return jobs.submit(payload)

    @app.get(
        "/api/jobs/{job_id}",
        response_model=JobStatus,
        responses={404: {"model": ErrorResponse}},
    )
    async def get_job(job_id: str) -> JobStatus:
        return jobs.get(job_id)

    @app.get(
        "/api/exports",
        response_model=ExportListResponse,
        responses={400: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
    )
    def list_exports(
        artifact_type: Literal["experiment", "multi_run"] | None = None,
    ) -> ExportListResponse:
        manifests: list[ExperimentManifest | MultiRunManifest] = []
        if artifact_type in (None, "experiment"):
            manifests.extend(discover_exports(root))
        if artifact_type in (None, "multi_run"):
            manifests.extend(discover_multi_run_exports(root))

        seen_ids: set[str] = set()
        for manifest in manifests:
            if manifest.export_id in seen_ids:
                raise _AmbiguousExportError(manifest.export_id)
            seen_ids.add(manifest.export_id)

        # Stable newest-first ordering; export ID breaks timestamp ties.
        manifests.sort(key=lambda manifest: manifest.export_id)
        manifests.sort(key=lambda manifest: manifest.exported_at, reverse=True)
        return ExportListResponse(
            exports=[
                ExportSummary(
                    export_id=manifest.export_id,
                    artifact_type=manifest.artifact_type,
                    exported_at=manifest.exported_at,
                    manifest=manifest,
                )
                for manifest in manifests
            ]
        )

    @app.get(
        "/api/exports/{export_id}",
        response_model=ExportDetailResponse,
        responses={
            400: {"model": ErrorResponse},
            404: {"model": ErrorResponse},
            409: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
        },
    )
    def get_export(export_id: str) -> ExportDetailResponse:
        matches: list[tuple[str, ExperimentArtifact | MultiRunArtifact]] = []
        errors: list[ArtifactReaderError] = []
        for artifact_type, loader in (
            ("experiment", load_export),
            ("multi_run", load_multi_run_export),
        ):
            try:
                matches.append((artifact_type, loader(export_id, root=root)))
            except ArtifactTypeMismatchError:
                continue
            except ArtifactReaderError as exc:
                errors.append(exc)

        if len(matches) > 1:
            raise _AmbiguousExportError(export_id)
        if errors:
            raise errors[0]
        if not matches:
            raise UnsupportedArtifactError("no reader accepted this artifact type")
        matched_type, artifact = matches[0]
        return ExportDetailResponse(
            export_id=export_id,
            artifact_type=matched_type,
            artifact=artifact,
        )

    return app
