from __future__ import annotations

import re
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

T = TypeVar("T")


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.page_size))


class Message(BaseModel):
    message: str
    ok: bool = True


_NG_LOCAL = re.compile(r"^0[7-9][01]\d{8}$")
_NG_INTL = re.compile(r"^\+234[7-9][01]\d{8}$")


def normalise_phone(value: str) -> str:
    """Normalise Nigerian numbers to E.164 (+234…).

    Accepts `08154471570`, `8154471570`, `2348154471570`, `+234 815 447 1570`.
    Anything already in `+<country><number>` form for another country is kept.
    """
    if not value:
        raise ValueError("Phone number is required")
    cleaned = re.sub(r"[\s\-()./]", "", value.strip())
    if cleaned.startswith("+"):
        if _NG_INTL.match(cleaned) or re.match(r"^\+\d{8,15}$", cleaned):
            return cleaned
        raise ValueError("That phone number doesn't look right")
    if cleaned.startswith("234") and len(cleaned) == 13:
        cleaned = "+" + cleaned
    elif _NG_LOCAL.match(cleaned):
        cleaned = "+234" + cleaned[1:]
    elif re.match(r"^[7-9][01]\d{8}$", cleaned):
        cleaned = "+234" + cleaned
    else:
        raise ValueError("That phone number doesn't look right")
    return cleaned


class PhoneMixin(BaseModel):
    @field_validator("phone", "passenger_phone", mode="before", check_fields=False)
    @classmethod
    def _normalise_phone(cls, v):
        if v is None or v == "":
            return v
        return normalise_phone(str(v))


class PaginationParams(BaseModel):
    page: int = Field(1, ge=1)
    page_size: int = Field(25, ge=1, le=200)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size
