from __future__ import annotations

import argparse
import asyncio
import http.client
import ipaddress
import logging
import socket
import ssl
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import or_, update
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Check, CheckResult

POLL_INTERVAL_SECONDS = 5
REQUEST_TIMEOUT_SECONDS = 5
TOTAL_CHECK_TIMEOUT_SECONDS = 10
MAX_CHECKS_PER_BATCH = 50
MAX_CONCURRENT_CHECKS = 10
USER_AGENT = "SignalLog-Monitor/1.0"
logger = logging.getLogger("signallog.monitor")


@dataclass(frozen=True)
class DueCheck:
    id: int
    target_url: str
    interval_seconds: int


class TargetError(ValueError):
    pass


def validate_target_url(url: str) -> tuple[str, int, str, bool]:
    if len(url) > 2048:
        raise TargetError("Target URL is too long.")
    if any(ord(character) < 32 or ord(character) == 127 for character in url):
        raise TargetError("Target URL contains invalid characters.")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise TargetError("Target URL is invalid.") from exc

    scheme = parsed.scheme.lower()
    host = parsed.hostname
    if scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
        raise TargetError("Target must be an HTTP or HTTPS URL without credentials.")
    expected_port = 443 if scheme == "https" else 80
    port = port or expected_port
    if port != expected_port:
        raise TargetError("Only standard HTTP and HTTPS ports are allowed.")
    if host.endswith(".") or host.lower() in {"localhost"} or host.lower().endswith((".localhost", ".local", ".internal")):
        raise TargetError("Target must resolve to a publicly routable host.")

    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        literal_ip = None
    if literal_ip is not None and not literal_ip.is_global:
        raise TargetError("Target must resolve to a publicly routable host.")

    path = parsed.path or "/"
    request_target = urlunsplit(("", "", path, parsed.query, ""))
    return host, port, request_target, scheme == "https"


def _public_addresses(host: str, port: int) -> list[str]:
    try:
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise TargetError("DNS lookup failed.") from exc

    addresses = list(dict.fromkeys(record[4][0] for record in records))
    if not addresses:
        raise TargetError("DNS lookup returned no addresses.")
    try:
        if any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise TargetError("Target must resolve to a publicly routable host.")
    except TargetError:
        raise
    except ValueError as exc:
        raise TargetError("DNS lookup returned an invalid address.") from exc
    return addresses


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, address: str) -> None:
        super().__init__(host, port, timeout=REQUEST_TIMEOUT_SECONDS)
        self.address = address

    def connect(self) -> None:
        self.sock = socket.create_connection((self.address, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, address: str) -> None:
        super().__init__(host, port, timeout=REQUEST_TIMEOUT_SECONDS, context=ssl.create_default_context())
        self.address = address

    def connect(self) -> None:
        raw_socket = socket.create_connection((self.address, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)
        except Exception:
            raw_socket.close()
            raise


def _request_one_address(url: str, address: str) -> int:
    host, port, target, use_tls = validate_target_url(url)
    connection_type = _PinnedHTTPSConnection if use_tls else _PinnedHTTPConnection
    connection = connection_type(host, port, address)
    try:
        connection.request(
            "GET",
            target,
            headers={
                "Host": host,
                "User-Agent": USER_AGENT,
                "Connection": "close",
            },
        )
        response = connection.getresponse()
        return response.status
    finally:
        connection.close()


def perform_check(url: str) -> tuple[str, str]:
    try:
        host, port, _, _ = validate_target_url(url)
        addresses = _public_addresses(host, port)
        last_error: OSError | None = None
        for address in addresses:
            try:
                status = _request_one_address(url, address)
                if 200 <= status < 400:
                    return "passed", f"HTTP {status}"
                return "failed", f"HTTP {status}"
            except (OSError, http.client.HTTPException, ssl.SSLError, TimeoutError) as exc:
                last_error = exc
        if isinstance(last_error, (TimeoutError, socket.timeout)):
            return "failed", "Request timed out."
        if last_error is not None:
            return "failed", "Could not connect to target."
        return "failed", "Could not connect to target."
    except TargetError as exc:
        return "failed", str(exc)


def _claim_due_checks(db: Session, now: datetime) -> list[DueCheck]:
    due = (
        db.query(Check)
        .filter(
            Check.is_disabled.is_(False),
            Check.target_url.is_not(None),
            or_(Check.next_check_at.is_(None), Check.next_check_at <= now),
        )
        .order_by(Check.next_check_at, Check.id)
        .limit(MAX_CHECKS_PER_BATCH)
        .all()
    )
    claimed: list[DueCheck] = []
    for check in due:
        claim = db.execute(
            update(Check)
            .where(
                Check.id == check.id,
                Check.is_disabled.is_(False),
                Check.target_url == check.target_url,
                Check.interval_seconds == check.interval_seconds,
                or_(Check.next_check_at.is_(None), Check.next_check_at <= now),
            )
            .values(next_check_at=now + timedelta(seconds=check.interval_seconds))
            .execution_options(synchronize_session=False)
        )
        if claim.rowcount == 1:
            claimed.append(DueCheck(check.id, check.target_url, check.interval_seconds))
    db.commit()
    return claimed


async def run_due_checks(now: datetime | None = None) -> int:
    run_at = now or datetime.now(timezone.utc)
    with SessionLocal() as db:
        due_checks = _claim_due_checks(db, run_at)

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)

    async def execute(check: DueCheck) -> None:
        async with semaphore:
            try:
                status, message = await asyncio.wait_for(
                    asyncio.to_thread(perform_check, check.target_url),
                    timeout=TOTAL_CHECK_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                status, message = "failed", "Request timed out."
            with SessionLocal() as db:
                db.add(
                    CheckResult(
                        check_id=check.id,
                        status=status,
                        source="monitor",
                        message=message[:1000],
                    )
                )
                db.execute(
                    update(Check)
                    .where(Check.id == check.id)
                    .values(last_checked_at=datetime.now(timezone.utc))
                    .execution_options(synchronize_session=False)
                )
                db.commit()
            logger.info(
                "monitor_result check_id=%s status=%s interval_seconds=%s",
                check.id,
                status,
                check.interval_seconds,
            )

    await asyncio.gather(*(execute(check) for check in due_checks))
    return len(due_checks)


async def run_worker() -> None:
    logger.info("monitor_started poll_interval_seconds=%s", POLL_INTERVAL_SECONDS)
    while True:
        try:
            await run_due_checks()
        except Exception:
            logger.exception("monitor_batch_failed")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run SignalLog's scheduled HTTP monitor.")
    parser.add_argument("--once", action="store_true", help="Run due checks once and exit.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.once:
        asyncio.run(run_due_checks())
    else:
        asyncio.run(run_worker())


if __name__ == "__main__":
    main()
