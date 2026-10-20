import asyncio
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import monitor
from app.database import Base, get_db
from app.main import MAX_REQUEST_BYTES, app
from app.models import Check, CheckResult

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
    now = datetime.now(UTC)
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
    monkeypatch.setattr(
        monitor,
        "perform_check",
        lambda url: monitor.CheckOutcome("passed", "HTTP 204", 204, 12),
    )

    assert asyncio.run(monitor.run_due_checks(now)) == 1
    assert asyncio.run(monitor.run_due_checks(now)) == 0

    db = TestSessionLocal()
    result = db.query(CheckResult).filter(CheckResult.check_id == check_id).one()
    updated_check = db.get(Check, check_id)
    assert result.status == "passed"
    assert result.source == "monitor"
    assert result.message == "HTTP 204"
    assert result.status_code == 204
    assert result.latency_ms == 12
    assert updated_check.last_checked_at is not None
    assert updated_check.next_check_at > now
    db.close()


def test_monitor_rejects_dns_results_with_private_addresses(monkeypatch):
    monkeypatch.setattr(
        monitor.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, ("10.0.0.4", 443))],
    )
    outcome = monitor.perform_check("https://example.com/health")
    assert outcome.status == "failed"
    assert "publicly routable" in outcome.message


def test_perform_check_uses_local_http_request_path_and_records_http_outcomes(monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        response_status = 200

        def do_GET(self):
            self.send_response(self.response_status)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, format, *args):
            pass

    for response_status, expected_status in ((204, "passed"), (302, "failed"), (404, "failed"), (503, "failed")):
        handler = type(f"Handler{response_status}", (Handler,), {"response_status": response_status})
        server = HTTPServer(("127.0.0.1", 0), handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        port = server.server_address[1]

        def local_target(url, port=port):
            parsed = urlsplit(url)
            return parsed.hostname or "example.test", port, parsed.path or "/", False

        monkeypatch.setattr(monitor, "validate_target_url", local_target)
        monkeypatch.setattr(monitor, "_public_addresses", lambda host, port: ["127.0.0.1"])
        try:
            outcome = monitor.perform_check(f"http://example.test:{port}/health")
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join()

        assert outcome.status == expected_status
        assert outcome.status_code == response_status
        assert outcome.latency_ms >= 0
        if response_status == 302:
            assert "redirect not followed" in outcome.message


def test_perform_check_records_timeout_without_http_status(monkeypatch):
    monkeypatch.setattr(
        monitor,
        "validate_target_url",
        lambda url: ("example.com", 80, "/health", False),
    )
    monkeypatch.setattr(monitor, "_public_addresses", lambda host, port: ["93.184.216.34"])

    def timeout(url, address):
        raise TimeoutError

    monkeypatch.setattr(monitor, "_request_one_address", timeout)
    outcome = monitor.perform_check("http://example.com/health")
    assert outcome.status == "failed"
    assert outcome.message == "Request timed out."
    assert outcome.status_code is None
    assert outcome.latency_ms >= 0


def test_perform_check_records_dns_failure_without_http_status(monkeypatch):
    def dns_failure(host, port):
        raise monitor.TargetError("DNS lookup failed.")

    monkeypatch.setattr(monitor, "_public_addresses", dns_failure)
    outcome = monitor.perform_check("https://example.com/health")
    assert outcome.status == "failed"
    assert outcome.message == "DNS lookup failed."
    assert outcome.status_code is None
    assert outcome.latency_ms >= 0


def test_sqlite_connections_enable_wal_busy_timeout_and_foreign_keys(tmp_path):
    sqlite_path = tmp_path / "settings.db"
    engine = create_engine(f"sqlite:///{sqlite_path}")
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
            assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar() == 5000
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
    finally:
        engine.dispose()


def test_foreign_key_restrict_is_enforced():
    db = TestSessionLocal()
    check = Check(name="Referenced check")
    db.add(check)
    db.commit()
    db.add(CheckResult(check_id=check.id, status="passed"))
    db.commit()

    with pytest.raises(IntegrityError):
        db.execute(text("DELETE FROM checks WHERE id = :check_id"), {"check_id": check.id})
        db.commit()
    db.rollback()
    assert db.get(Check, check.id) is not None
    db.close()


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
    assert created_result.json()["status_code"] is None
    assert created_result.json()["latency_ms"] is None

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

    from_dt = datetime(2026, 1, 1, tzinfo=UTC)
    to_dt = datetime(2026, 1, 3, tzinfo=UTC)
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
    assert payload["manual"] == {"total": 2, "passed": 1, "failed": 1}
    assert payload["monitor"] == {"total": 0, "passed": 0, "failed": 0}


def test_overview_range_queries_use_result_indexes():
    db = TestSessionLocal()
    range_plan = db.execute(
        text(
            "EXPLAIN QUERY PLAN SELECT source, status, COUNT(*) FROM check_results "
            "WHERE recorded_at >= :from_dt AND recorded_at < :to_dt "
            "GROUP BY source, status"
        ),
        {"from_dt": "2026-01-01", "to_dt": "2026-02-01"},
    ).all()
    check_plan = db.execute(
        text(
            "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM check_results "
            "WHERE check_id = :check_id AND recorded_at >= :from_dt AND recorded_at < :to_dt"
        ),
        {"check_id": 1, "from_dt": "2026-01-01", "to_dt": "2026-02-01"},
    ).all()
    db.close()

    assert any("ix_check_results_recorded_at" in row[3] for row in range_plan)
    assert any("ix_check_results_check_id_recorded_at" in row[3] for row in check_plan)


def test_overview_uses_source_and_status_aggregates_and_joins_failures():
    db = TestSessionLocal()
    check = Check(name="Source counts")
    db.add(check)
    db.commit()
    now = datetime.now(UTC)
    db.add_all(
        [
            CheckResult(check_id=check.id, status="passed", source="manual", recorded_at=now),
            CheckResult(check_id=check.id, status="failed", source="monitor", message="HTTP 500", recorded_at=now),
        ]
    )
    db.commit()
    db.close()

    response = client.get(
        "/overview",
        params={
            "from": (now - timedelta(minutes=1)).isoformat(),
            "to": (now + timedelta(minutes=1)).isoformat(),
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert payload["manual"] == {"total": 1, "passed": 1, "failed": 0}
    assert payload["monitor"] == {"total": 1, "passed": 0, "failed": 1}
    assert payload["recent_failures"][0]["check_name"] == "Source counts"


def test_migrations_downgrade_and_upgrade_again():
    db = TestSessionLocal()
    check = Check(name="Migration round trip", target_url="https://example.com/health")
    db.add(check)
    db.commit()
    db.add(
        CheckResult(
            check_id=check.id,
            status="failed",
            source="monitor",
            message="HTTP 503",
            status_code=503,
            latency_ms=42,
        )
    )
    db.commit()
    db.close()

    alembic_config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with test_engine.connect() as connection:
        alembic_config.attributes["connection"] = connection
        command.downgrade(alembic_config, "base")
        inspector = inspect(connection)
        assert "target_url" not in {column["name"] for column in inspector.get_columns("checks")}
        assert "source" not in {column["name"] for column in inspector.get_columns("check_results")}
        assert "status_code" not in {column["name"] for column in inspector.get_columns("check_results")}
        assert connection.execute(text("SELECT COUNT(*) FROM check_results")).scalar_one() == 1

        command.upgrade(alembic_config, "head")
        inspector = inspect(connection)
        assert {"target_url", "interval_seconds"} <= {
            column["name"] for column in inspector.get_columns("checks")
        }
        assert {"source", "status_code", "latency_ms"} <= {
            column["name"] for column in inspector.get_columns("check_results")
        }
        restored_result = connection.execute(
            text("SELECT status, source, status_code, latency_ms FROM check_results")
        ).one()
        assert restored_result == ("failed", "manual", None, None)
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []


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
            from sqlalchemy.exc import SQLAlchemyError

            raise SQLAlchemyError("database unavailable")

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
