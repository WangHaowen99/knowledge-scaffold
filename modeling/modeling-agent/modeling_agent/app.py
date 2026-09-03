from __future__ import annotations

import json
import secrets
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from pydantic import BaseModel, Field, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from .errors import AgentRuntimeError
from .service import AgentService


class StartRunBody(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)
    baselineRevision: str = Field(default="", max_length=200)


class SteerBody(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)
    purposeReviewCompleted: bool = False


class ResumeIssueBody(BaseModel):
    actionId: str = Field(min_length=1, max_length=100)


class AnswerBody(BaseModel):
    answer: dict[str, Any]


class MaterialEventBody(BaseModel):
    eventId: str = Field(min_length=1, max_length=200)
    status: str = Field(pattern="^(parsing|parsed|failed)$")
    documentId: str = Field(min_length=1, max_length=200)
    sourceKey: str = Field(min_length=1, max_length=300)
    filename: str = Field(min_length=1, max_length=500)
    revisionNo: int = Field(ge=1)
    purpose: str = Field(pattern="^(auto|goal|schema|term|evidence)$")
    readingMode: str = Field(pattern="^(on_demand|full)$")
    autoContinue: bool = True
    uploadBatchId: str = Field(default="", max_length=200)
    uploadBatchSize: int = Field(default=1, ge=1, le=100)
    parseError: str = Field(default="", max_length=4_000)
    changeKind: str = Field(default="", max_length=50)
    semanticChanged: bool | None = None
    purposeReviewRequired: bool = False
    sheetCount: int = Field(default=0, ge=0, le=10_000)


class StudioReviewEventBody(BaseModel):
    eventId: str = Field(min_length=1, max_length=500)
    clientMutationId: str = Field(min_length=1, max_length=128)
    requestHash: str = Field(min_length=64, max_length=64)
    workingRevision: str = Field(min_length=1, max_length=200)
    changedPaths: list[str] = Field(min_length=1, max_length=2_000)
    elementIds: list[str] = Field(default_factory=list, max_length=200)
    summary: str = Field(min_length=1, max_length=2_000)


class FusionReviewEventBody(BaseModel):
    eventId: str = Field(min_length=1, max_length=500)
    planId: str = Field(min_length=1, max_length=200)
    candidateId: str = Field(min_length=1, max_length=200)
    outputName: str = Field(min_length=1, max_length=200)
    sourceCount: int = Field(ge=2, le=20)
    summary: str = Field(min_length=1, max_length=2_000)


class PackageImportEventBody(BaseModel):
    eventId: str = Field(min_length=1, max_length=500)
    baselineRevision: str = Field(min_length=1, max_length=200)
    packageName: str = Field(min_length=1, max_length=500)
    packageVersion: str = Field(min_length=1, max_length=100)
    sourceFilename: str = Field(min_length=1, max_length=500)
    fileCount: int = Field(ge=1, le=100_000)


class EvaluationTerminalEventBody(BaseModel):
    eventId: str = Field(min_length=1, max_length=500)
    roundId: str = Field(min_length=1, max_length=200)
    status: str = Field(pattern="^(done|failed|cancelled)$")
    done: int = Field(ge=0)
    total: int = Field(ge=0)
    passed: int = Field(ge=0)
    finishedAt: str = Field(min_length=1, max_length=100)


class ProjectTransferBody(BaseModel):
    payload: dict[str, Any]


def create_app(service: AgentService) -> Starlette:
    async def health(_request: Request) -> JSONResponse:
        return JSONResponse(service.health())

    async def skills(_request: Request) -> JSONResponse:
        return JSONResponse({"skills": service.health()["skills"]})

    async def state(request: Request) -> JSONResponse:
        _require_token(request)
        return JSONResponse(service.project_state(request.path_params["project_id"]))

    async def start(request: Request) -> JSONResponse:
        body = _validate(StartRunBody, await _body(request))
        run = await service.start_run(
            project_id=request.path_params["project_id"],
            token=_require_token(request),
            message=body.message,
            baseline_revision=body.baselineRevision,
        )
        return JSONResponse({"run": run}, status_code=202)

    async def steer(request: Request) -> JSONResponse:
        body = _validate(SteerBody, await _body(request))
        result = await service.steer(
            project_id=request.path_params["project_id"],
            token=_require_token(request),
            text=body.message,
            purpose_review_completed=body.purposeReviewCompleted,
        )
        return JSONResponse(result, status_code=202)

    async def resume_issue(request: Request) -> JSONResponse:
        body = _validate(ResumeIssueBody, await _body(request))
        result = await service.resume_issue(
            project_id=request.path_params["project_id"],
            token=_require_token(request),
            issue_id=request.path_params["issue_id"],
            action_id=body.actionId,
        )
        return JSONResponse(result, status_code=202)

    async def answer(request: Request) -> JSONResponse:
        body = _validate(AnswerBody, await _body(request))
        run = await service.answer_interaction(
            project_id=request.path_params["project_id"],
            interaction_id=request.path_params["interaction_id"],
            token=_require_token(request),
            answer=body.answer,
        )
        return JSONResponse({"run": run}, status_code=202)

    async def cancel(request: Request) -> JSONResponse:
        _require_token(request)
        run = await service.cancel(project_id=request.path_params["project_id"])
        return JSONResponse({"run": run})

    async def terminate_stale(request: Request) -> JSONResponse:
        _require_token(request)
        result = await service.safe_terminate_stale(project_id=request.path_params["project_id"])
        return JSONResponse(result)

    async def material_event(request: Request) -> JSONResponse:
        body = _validate(MaterialEventBody, await _body(request))
        result = await service.handle_material_event(
            project_id=request.path_params["project_id"],
            token=_require_token(request),
            event=body.model_dump(),
        )
        return JSONResponse(result, status_code=202)

    async def studio_event(request: Request) -> JSONResponse:
        body = _validate(StudioReviewEventBody, await _body(request))
        result = await service.handle_studio_review_event(
            project_id=request.path_params["project_id"],
            token=_require_token(request),
            event=body.model_dump(),
        )
        return JSONResponse(result, status_code=202)

    async def fusion_event(request: Request) -> JSONResponse:
        body = _validate(FusionReviewEventBody, await _body(request))
        result = await service.handle_fusion_review_event(
            project_id=request.path_params["project_id"],
            token=_require_token(request),
            event=body.model_dump(),
        )
        return JSONResponse(result, status_code=202)

    async def package_import_event(request: Request) -> JSONResponse:
        body = _validate(PackageImportEventBody, await _body(request))
        result = await service.handle_package_import_event(
            project_id=request.path_params["project_id"],
            token=_require_token(request),
            event=body.model_dump(),
        )
        return JSONResponse(result, status_code=202)

    async def evaluation_event(request: Request) -> JSONResponse:
        body = _validate(EvaluationTerminalEventBody, await _body(request))
        result = await service.handle_evaluation_event(
            project_id=request.path_params["project_id"],
            token=_require_token(request),
            event=body.model_dump(),
        )
        return JSONResponse(result, status_code=202)

    async def export_project(request: Request) -> JSONResponse:
        _require_internal_access(request, service.settings.service_token)
        return JSONResponse(service.export_project_transfer(request.path_params["project_id"]))

    async def import_project(request: Request) -> JSONResponse:
        _require_internal_access(request, service.settings.service_token)
        body = _validate(ProjectTransferBody, await _body(request))
        id_map = service.import_project_transfer(request.path_params["project_id"], body.payload)
        return JSONResponse({"ok": True, "idMap": id_map}, status_code=201)

    async def delete_project(request: Request) -> JSONResponse:
        _require_internal_access(request, service.settings.service_token)
        service.delete_project_transfer(request.path_params["project_id"])
        return JSONResponse({"ok": True})

    async def stream(request: Request) -> StreamingResponse:
        _require_token(request)
        project_id = request.path_params["project_id"]
        after = _after_sequence(request)

        async def frames():
            async for event in service.events.stream(project_id, after):
                if event is None:
                    yield b": keep-alive\n\n"
                    continue
                data = json.dumps(event.public(), ensure_ascii=False, separators=(",", ":"))
                yield f"id: {event.seq}\nevent: {event.kind}\ndata: {data}\n\n".encode("utf-8")

        return StreamingResponse(
            frames(),
            media_type="text/event-stream; charset=utf-8",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        await service.startup()
        try:
            yield
        finally:
            await service.shutdown()

    routes = [
        Route("/health", health, methods=["GET"]),
        Route("/v1/skills", skills, methods=["GET"]),
        Route("/v1/projects/{project_id}/state", state, methods=["GET"]),
        Route("/v1/projects/{project_id}/runs", start, methods=["POST"]),
        Route("/v1/projects/{project_id}/steer", steer, methods=["POST"]),
        Route("/v1/projects/{project_id}/issues/{issue_id}/resume", resume_issue, methods=["POST"]),
        Route("/v1/projects/{project_id}/cancel", cancel, methods=["POST"]),
        Route("/v1/projects/{project_id}/recovery/terminate-stale", terminate_stale, methods=["POST"]),
        Route("/v1/projects/{project_id}/stream", stream, methods=["GET"]),
        Route("/v1/projects/{project_id}/interactions/{interaction_id}/answer", answer, methods=["POST"]),
        Route("/v1/internal/projects/{project_id}/material-events", material_event, methods=["POST"]),
        Route("/v1/internal/projects/{project_id}/studio-events", studio_event, methods=["POST"]),
        Route("/v1/internal/projects/{project_id}/fusion-events", fusion_event, methods=["POST"]),
        Route("/v1/internal/projects/{project_id}/package-import-events", package_import_event, methods=["POST"]),
        Route("/v1/internal/projects/{project_id}/evaluation-events", evaluation_event, methods=["POST"]),
        Route("/v1/internal/projects/{project_id}/transfer", export_project, methods=["GET"]),
        Route("/v1/internal/projects/{project_id}/transfer", import_project, methods=["POST"]),
        Route("/v1/internal/projects/{project_id}/transfer", delete_project, methods=["DELETE"]),
    ]
    return Starlette(
        routes=routes,
        lifespan=lifespan,
        exception_handlers={AgentRuntimeError: _runtime_error},
    )


async def _runtime_error(_request: Request, error: AgentRuntimeError) -> JSONResponse:
    return JSONResponse(error.to_dict(), status_code=error.status)


async def _body(request: Request) -> dict[str, Any]:
    try:
        value = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise AgentRuntimeError("INVALID_JSON", "请求体不是合法 JSON") from error
    if not isinstance(value, dict):
        raise AgentRuntimeError("INVALID_INPUT", "请求体必须是对象")
    return value


def _validate(model: type[BaseModel], value: dict[str, Any]):
    try:
        return model.model_validate(value)
    except ValidationError as error:
        detail = error.errors()[0].get("msg", "输入不完整")
        raise AgentRuntimeError("INVALID_INPUT", f"输入不正确：{detail}") from error


def _require_token(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    if not token:
        raise AgentRuntimeError("TOKEN_REQUIRED", "需要登录后才能使用建模助手", status=401)
    return token


def _require_internal_access(request: Request, expected_service_token: str) -> None:
    expected = expected_service_token.strip()
    supplied = request.headers.get("x-modeling-service-token", "").strip()
    if expected:
        if supplied and secrets.compare_digest(supplied, expected):
            return
        raise AgentRuntimeError("UNAUTHORIZED", "内部服务认证失败", status=401)
    _require_token(request)


def _after_sequence(request: Request) -> int:
    raw = request.query_params.get("after") or request.headers.get("last-event-id") or "0"
    try:
        value = int(raw)
    except ValueError:
        value = 0
    return max(0, value)
