from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import settings
from app.database import get_db
from app.models import Check, CheckResult
from app.monitor import TargetError, validate_target_url
from app.schemas import (
    CheckCreate,
    CheckPatch,
    CheckResponse,
    OverviewResponse,
    ResultCreate,
    ResultResponse,
)

app = FastAPI(title=settings.app_name)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["localhost", "127.0.0.1"],
)

MAX_REQUEST_BYTES = 16 * 1024


class RequestLimitsMiddleware:
    def __init__(self, application: ASGIApp) -> None:
        self.application = application

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in {"POST", "PATCH"}:
            await self.application(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope["headers"]}
        content_type = headers.get(b"content-type", b"").split(b";", 1)[0].strip().lower()
        if content_type != b"application/json":
            response = JSONResponse(
                status_code=415,
                content=error_payload("unsupported_media_type", "Write requests must use application/json"),
            )
            await response(scope, receive, send)
            return

        try:
            content_length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            content_length = 0
        if content_length > MAX_REQUEST_BYTES:
            response = JSONResponse(
                status_code=413,
                content=error_payload("payload_too_large", "Request body exceeds 16 KB"),
            )
            await response(scope, receive, send)
            return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > MAX_REQUEST_BYTES:
                response = JSONResponse(
                    status_code=413,
                    content=error_payload("payload_too_large", "Request body exceeds 16 KB"),
                )
                await response(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        body_sent = False

        async def replay_body() -> dict[str, Any]:
            nonlocal body_sent
            if body_sent:
                return {"type": "http.request", "body": b"", "more_body": False}
            body_sent = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.application(scope, replay_body, send)


app.add_middleware(RequestLimitsMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.cors_origin],
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["Content-Type"],
)


def error_payload(code: str, message: str, details: list[str] | None = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details or []}}


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    details: list[str] = []
    for err in exc.errors():
        location = ".".join(str(part) for part in err.get("loc", ()))
        details.append(f"{location}: {err.get('msg', 'Invalid value')}")
    return JSONResponse(status_code=422, content=error_payload("validation_error", "Request validation failed", details))


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, dict) else {"code": "internal_error", "message": str(exc.detail or "Unknown error"), "details": []}
    payload = detail if detail.get("code") else {"code": "internal_error", "message": detail.get("message", str(exc.detail or "Unknown error")), "details": detail.get("details", [])}
    return JSONResponse(status_code=exc.status_code, content={"error": payload})


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=500, content=error_payload("internal_error", "Internal server error"))


@app.get("/health/live")
def health_live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
def health_ready(db: Session = Depends(get_db)) -> dict[str, str]:
    try:
        db.execute(text("SELECT 1"))
        return {"status": "ready"}
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail={"code": "not_ready", "message": "Database is unavailable", "details": []})


def parse_datetime(value: str | None, *, default: datetime | None = None) -> datetime:
    if value is None:
        if default is not None:
            return default
        raise HTTPException(status_code=422, detail={"code": "validation_error", "message": "A datetime value is required", "details": []})
    try:
        candidate = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "validation_error", "message": "Invalid datetime format", "details": [str(exc)]}) from exc
    if parsed.tzinfo is None:
        raise HTTPException(status_code=422, detail={"code": "validation_error", "message": "Naive datetimes are rejected", "details": []})
    return parsed.astimezone(UTC)


@app.get("/checks", response_model=dict)
def list_checks(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    query = db.query(Check).order_by(Check.created_at.desc(), Check.id.desc())
    total = query.count()
    items = query.offset(offset).limit(limit).all()
    return {"items": [CheckResponse.model_validate(item).model_dump(mode="json") for item in items], "limit": limit, "offset": offset, "total": total}


@app.post("/checks", response_model=CheckResponse, status_code=201)
def create_check(payload: CheckCreate, db: Session = Depends(get_db)) -> Check:
    if payload.target_url:
        try:
            validate_target_url(payload.target_url)
        except TargetError as exc:
            raise HTTPException(status_code=422, detail={"code": "validation_error", "message": str(exc), "details": []}) from exc
    existing = db.query(Check).filter(func.lower(Check.name) == payload.name.lower()).first()
    if existing:
        raise HTTPException(status_code=409, detail={"code": "duplicate_name", "message": "Check name already exists", "details": []})

    item = Check(
        name=payload.name.strip(),
        description=payload.description.strip() if payload.description else None,
        target_url=payload.target_url,
        interval_seconds=payload.interval_seconds,
        next_check_at=datetime.now(UTC) if payload.target_url and not payload.is_disabled else None,
        is_disabled=payload.is_disabled,
    )
    db.add(item)
    try:
        db.commit()
        db.refresh(item)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail={"code": "duplicate_name", "message": "Check name already exists", "details": []}) from exc
    return item


@app.patch("/checks/{check_id}", response_model=CheckResponse)
def patch_check(check_id: int, payload: CheckPatch, db: Session = Depends(get_db)) -> Check:
    item = db.get(Check, check_id)
    if item is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Check not found", "details": []})

    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail={"code": "validation_error", "message": "Patch body is empty", "details": []})

    if "target_url" in changes and changes["target_url"] is not None:
        try:
            validate_target_url(changes["target_url"])
        except TargetError as exc:
            raise HTTPException(status_code=422, detail={"code": "validation_error", "message": str(exc), "details": []}) from exc
    if "name" in changes and changes["name"] is not None:
        normalized = changes["name"]
        duplicate = db.query(Check).filter(Check.id != check_id, func.lower(Check.name) == normalized.lower()).first()
        if duplicate:
            raise HTTPException(status_code=409, detail={"code": "duplicate_name", "message": "Check name already exists", "details": []})
        item.name = normalized
    if "description" in changes:
        item.description = changes["description"].strip() if changes["description"] else None
    if "is_disabled" in changes:
        if changes["is_disabled"] is None:
            raise HTTPException(status_code=422, detail={"code": "validation_error", "message": "is_disabled cannot be null", "details": []})
        item.is_disabled = bool(changes["is_disabled"])
    if "interval_seconds" in changes:
        if changes["interval_seconds"] is None:
            raise HTTPException(status_code=422, detail={"code": "validation_error", "message": "interval_seconds cannot be null", "details": []})
        item.interval_seconds = changes["interval_seconds"]
    if "target_url" in changes:
        item.target_url = changes["target_url"]

    if {"is_disabled", "interval_seconds", "target_url"} & changes.keys():
        item.next_check_at = (
            datetime.now(UTC)
            if item.target_url is not None and not item.is_disabled
            else None
        )

    try:
        db.commit()
        db.refresh(item)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail={"code": "duplicate_name", "message": "Check name already exists", "details": []}) from exc
    return item


@app.get("/results", response_model=dict)
def list_results(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    query = db.query(CheckResult).order_by(CheckResult.recorded_at.desc(), CheckResult.id.desc())
    total = query.count()
    items = query.offset(offset).limit(limit).all()
    return {"items": [ResultResponse.model_validate(item).model_dump(mode="json") for item in items], "limit": limit, "offset": offset, "total": total}


@app.post("/results", response_model=ResultResponse, status_code=201)
def create_result(payload: ResultCreate, db: Session = Depends(get_db)) -> CheckResult:
    if payload.recorded_at is not None:
        raise HTTPException(status_code=422, detail={"code": "validation_error", "message": "Client-supplied recorded_at is not allowed", "details": []})

    check = db.get(Check, payload.check_id)
    if check is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Check not found", "details": []})
    if check.is_disabled:
        raise HTTPException(status_code=409, detail={"code": "check_disabled", "message": "Check is disabled", "details": []})

    item = CheckResult(check_id=check.id, status=payload.status, message=payload.message.strip() if payload.message else None)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@app.get("/overview", response_model=OverviewResponse)
def overview(
    from_dt: str | None = Query(default=None, alias="from"),
    to_dt: str | None = Query(default=None, alias="to"),
    check_id: int | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    request_time = datetime.now(UTC)
    to_value = parse_datetime(to_dt, default=request_time)
    from_value = parse_datetime(from_dt, default=(to_value - timedelta(days=30)))
    if from_value >= to_value:
        raise HTTPException(status_code=422, detail={"code": "validation_error", "message": "from must be earlier than to", "details": []})
    if (to_value - from_value) > timedelta(days=366):
        raise HTTPException(status_code=422, detail={"code": "validation_error", "message": "Range exceeds 366 days", "details": []})

    if check_id is not None:
        check = db.get(Check, check_id)
        if check is None:
            raise HTTPException(status_code=404, detail={"code": "not_found", "message": "Check not found", "details": []})

    stmt = db.query(CheckResult.source, CheckResult.status, func.count(CheckResult.id)).filter(
        CheckResult.recorded_at >= from_value,
        CheckResult.recorded_at < to_value,
    )
    if check_id is not None:
        stmt = stmt.filter(CheckResult.check_id == check_id)

    source_counts = {
        "manual": {"total": 0, "passed": 0, "failed": 0},
        "monitor": {"total": 0, "passed": 0, "failed": 0},
    }
    for source, status, count in stmt.group_by(CheckResult.source, CheckResult.status).all():
        source_counts[source]["total"] += count
        source_counts[source][status] += count

    total = sum(counts["total"] for counts in source_counts.values())
    passed = sum(counts["passed"] for counts in source_counts.values())
    failed = sum(counts["failed"] for counts in source_counts.values())
    pass_rate = round((passed / total) * 100, 2) if total else None

    failure_query = (
        db.query(CheckResult, Check.name)
        .join(Check, Check.id == CheckResult.check_id)
        .filter(
            CheckResult.recorded_at >= from_value,
            CheckResult.recorded_at < to_value,
            CheckResult.status == "failed",
        )
    )
    if check_id is not None:
        failure_query = failure_query.filter(CheckResult.check_id == check_id)
    recent_failures = [
        {
            "check_id": result.check_id,
            "check_name": check_name,
            "message": result.message,
            "recorded_at": result.recorded_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        }
        for result, check_name in failure_query.order_by(
            CheckResult.recorded_at.desc(),
            CheckResult.id.desc(),
        ).limit(10)
    ]

    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "pass_rate": pass_rate,
        "manual": source_counts["manual"],
        "monitor": source_counts["monitor"],
        "recent_failures": recent_failures,
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
