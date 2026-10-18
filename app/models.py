from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.types import UTCDateTime


class Check(Base):
    __tablename__ = "checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    target_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=60, server_default="60")
    next_check_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    is_disabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=func.now(), onupdate=func.now(), nullable=False)

    results: Mapped[list["CheckResult"]] = relationship(back_populates="check")

    __table_args__ = (
        CheckConstraint(
            "interval_seconds >= 60 AND interval_seconds <= 86400",
            name="cc_check_interval_range",
        ),
        Index("ix_checks_name_lower", func.lower(name), unique=True),
    )


class CheckResult(Base):
    __tablename__ = "check_results"

    __table_args__ = (
        CheckConstraint("status IN ('passed', 'failed')", name="cc_check_result_status"),
        CheckConstraint("source IN ('manual', 'monitor')", name="cc_check_result_source"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    check_id: Mapped[int] = mapped_column(ForeignKey("checks.id", ondelete="RESTRICT"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    source: Mapped[str] = mapped_column(String(10), nullable=False, default="manual", server_default="manual")
    message: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=func.now(), nullable=False)

    check: Mapped[Check] = relationship(back_populates="results")
