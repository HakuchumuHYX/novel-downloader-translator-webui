from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EnvImportRequest(BaseModel):
    env_text: str = ""


class CookieJsonParseRequest(BaseModel):
    raw_text: str = ""


class CookieProfileUpsertRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = ""
    site: str = ""
    cookie: str = ""
    profile_id: int | None = None


class TaskTemplateCreateRequest(BaseModel):
    name: str = Field(min_length=1)
    payload: dict[str, Any]
