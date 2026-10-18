import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.main import app
from app import monitor
from app.models import Check, CheckResult
from app.main import MAX_REQUEST_BYTES

test_engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


def override_get_db():
    db = TestSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(autouse=True)
def use_isolated_database(monkeypatch):
    Base.metadata.drop_all(bind=test_engine)
    alembic_config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with test_engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
        alembic_config.attributes["connection"] = connection
        command.upgrade(alembic_config, "head")
    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr(monitor, "SessionLocal", TestSessionLocal)
    yield
    app.dependency_overrides.pop(get_db, None)
    Base.metadata.drop_all(bind=test_engine)


client = TestClient(app, base_url="http://localhost")


def test_duplicate_name_is_case_insensitive():
    response = client.post("/checks", json={"name": "CPU Check", "description": " status "})
    assert response.status_code == 201

    duplicate = client.post("/checks", json={"name": " cpu check "})
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "duplicate_name"


def test_automated_check_accepts_public_url_and_defaults_to_one_minute():
    response = client.post(
        "/checks",
        json={"name": "Public endpoint", "target_url": "https://example.com/health"},
    )
    assert response.status_code == 201
    check = response.json()
    assert check["target_url"] == "https://example.com/health"
    assert check["interval_seconds"] == 60
    assert check["next_check_at"] is not None


def test_monitor_interval_can_be_updated_per_check():
    created = client.post(
        "/checks",
        json={"name": "Adjustable endpoint", "target_url": "https://example.com/health"},
    )
    updated = client.patch(f"/checks/{created.json()['id']}", json={"interval_seconds": 300})
    assert updated.status_code == 200
    assert updated.json()["interval_seconds"] == 300
    assert updated.json()["next_check_at"] is not None


@pytest.mark.parametrize(
    "target_url",
    [
        "ftp://example.com/health",
        "http://127.0.0.1/health",
        "http://localhost/health",
        "http://example.com:8080/health",
        "https://user:password@example.com/health",
    ],
)
def test_automated_check_rejects_unsafe_target_urls(target_url):
    response = client.post(
        "/checks",
        json={"name": f"Invalid endpoint {target_url}", "target_url": target_url},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_monitor_records_due_result_and_respects_interval(monkeypatch):
    now = datetime.now(timezone.utc)
    db = TestSessionLocal()
    check = Check(
        name="Scheduled endpoint",
        target_url="https://example.com/health",
        interval_seconds=60,
        next_check_at=now - timedelta(seconds=1),
    )
    db.add(check)
    db.commit()
    check_id = check.id
    db.close()
    monkeypatch.setattr(monitor, "perform_check", lambda url: ("passed", "HTTP 204"))

    assert asyncio.run(monitor.run_due_checks(now)) == 1
    assert asyncio.run(monitor.run_due_checks(now)) == 0

    db = TestSessionLocal()
    result = db.query(CheckResult).filter(CheckResult.check_id == check_id).one()
    updated_check = db.get(Check, check_id)
    assert result.status == "passed"
    assert result.source == "monitor"
    assert result.message == "HTTP 204"
    assert updated_check.last_checked_at is not None
    assert updated_check.next_check_at > now
    db.close()


def test_monitor_rejects_dns_results_with_private_addresses(monkeypatch):
    monkeypatch.setattr(
        monitor.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, ("10.0.0.4", 443))],
    )
    status, message = monitor.perform_check("https://example.com/health")
    assert status == "failed"
    assert "publicly routable" in message


def test_check_and_result_lists_serialize_database_models():
    created_check = client.post("/checks", json={"name": "Serializable Check"})
    assert created_check.status_code == 201
    check_id = created_check.json()["id"]

    created_result = client.post(
        "/results",
        json={"check_id": check_id, "status": "passed", "message": "all good"},
    )
    assert created_result.status_code == 201
    assert created_result.json()["check_id"] == check_id
    assert created_result.json()["source"] == "manual"

    checks = client.get("/checks")
    results = client.get("/results")
    assert checks.status_code == 200
    assert checks.json()["items"][0]["name"] == "Serializable Check"
    assert results.status_code == 200
    assert results.json()["items"][0]["message"] == "all good"


def test_range_boundaries_are_half_open():
    db = TestSessionLocal()
    check = Check(name="Range Check", description="test")
    db.add(check)
    db.commit()
    db.refresh(check)

    from_dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
    to_dt = datetime(2026, 1, 3, tzinfo=timezone.utc)
    db.add_all(
        [
            CheckResult(check_id=check.id, status="passed", message="included", recorded_at=from_dt),
            CheckResult(check_id=check.id, status="failed", message="included", recorded_at=from_dt + timedelta(hours=1)),
            CheckResult(check_id=check.id, status="failed", message="excluded", recorded_at=to_dt),
        ]
    )
    db.commit()
    db.close()

    response = client.get("/overview", params={"from": from_dt.isoformat().replace("+00:00", "Z"), "to": to_dt.isoformat().replace("+00:00", "Z")})
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert payload["passed"] == 1
    assert payload["failed"] == 1
    assert payload["pass_rate"] == 50.0


def test_check_disabled_returns_409_for_result_submission():
    db = TestSessionLocal()
    check = Check(name="Disabled Check", is_disabled=True)
    db.add(check)
    db.commit()
    db.refresh(check)
    db.close()

    response = client.post("/results", json={"check_id": check.id, "status": "failed", "message": "It failed"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "check_disabled"


def test_missing_check_returns_404():
    response = client.post("/results", json={"check_id": 999, "status": "passed", "message": "missing"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_recorded_at_is_rejected_when_supplied_by_client():
    db = TestSessionLocal()
    check = Check(name="Timestamp Check")
    db.add(check)
    db.commit()
    db.refresh(check)
    db.close()

    response = client.post(
        "/results",
        json={"check_id": check.id, "status": "passed", "message": "ok", "recorded_at": "2026-01-01T00:00:00Z"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_cors_allows_only_the_configured_frontend_origin():
    allowed = client.options(
        "/checks",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:3000"

    denied = client.options(
        "/checks",
        headers={
            "Origin": "http://example.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert denied.headers.get("access-control-allow-origin") is None


def test_write_routes_reject_non_json_and_oversized_bodies():
    non_json = client.post(
        "/checks",
        content="name=unexpected",
        headers={"Content-Type": "text/plain"},
    )
    oversized = client.post(
        "/checks",
        content=b" " * (MAX_REQUEST_BYTES + 1),
        headers={"Content-Type": "application/json"},
    )
    assert non_json.status_code == 415
    assert non_json.json()["error"]["code"] == "unsupported_media_type"
    assert oversized.status_code == 413
    assert oversized.json()["error"]["code"] == "payload_too_large"


def test_api_rejects_untrusted_host_header():
    untrusted_client = TestClient(app, base_url="http://example.com")
    response = untrusted_client.get("/health/live")
    assert response.status_code == 400


def test_ready_health_returns_503_when_db_is_unavailable():
    from app import main as main_module

    class BoomSession:
        def execute(self, *args, **kwargs):
            raise RuntimeError("database unavailable")

    def failing_get_db():
        return BoomSession()

    previous_override = app.dependency_overrides[main_module.get_db]
    app.dependency_overrides[main_module.get_db] = failing_get_db
    try:
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "not_ready"
    finally:
        app.dependency_overrides[main_module.get_db] = previous_override
