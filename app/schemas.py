from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MIN_INTERVAL_SECONDS = 60
MAX_INTERVAL_SECONDS = 86_400


def normalize_target_url(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if len(value) > 2048:
        raise ValueError("target_url must be at most 2048 characters")
    return value


class CheckCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    target_url: str | None = Field(default=None, max_length=2048)
    interval_seconds: int = Field(default=MIN_INTERVAL_SECONDS, ge=MIN_INTERVAL_SECONDS, le=MAX_INTERVAL_SECONDS)
    is_disabled: bool = False

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("name must not be blank")
        return trimmed

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            return None
        return trimmed

    @field_validator("target_url")
    @classmethod
    def validate_target_url(cls, value: str | None) -> str | None:
        return normalize_target_url(value)


class CheckPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    target_url: str | None = Field(default=None, max_length=2048)
    interval_seconds: int | None = Field(default=None, ge=MIN_INTERVAL_SECONDS, le=MAX_INTERVAL_SECONDS)
    is_disabled: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_empty_body(cls, values):
        if values is None or values == {}:
            raise ValueError("empty patch body is not allowed")
        return values

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("name must not be blank")
        return trimmed

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        return None if not trimmed else trimmed

    @field_validator("target_url")
    @classmethod
    def validate_target_url(cls, value: str | None) -> str | None:
        return normalize_target_url(value)


class ResultCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_id: int
    status: Literal["passed", "failed"]
    message: str | None = Field(default=None, max_length=1000)
    recorded_at: datetime | None = None

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        return None if not trimmed else trimmed


class CheckResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None = None
    target_url: str | None = None
    interval_seconds: int
    next_check_at: datetime | None = None
    last_checked_at: datetime | None = None
    is_disabled: bool
    created_at: datetime
    updated_at: datetime


class ResultResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    check_id: int
    status: Literal["passed", "failed"]
    source: Literal["manual", "monitor"]
    message: str | None = None
    recorded_at: datetime


class OverviewResponse(BaseModel):
    total: int
    passed: int
    failed: int
    pass_rate: float | None
    recent_failures: list[dict]
