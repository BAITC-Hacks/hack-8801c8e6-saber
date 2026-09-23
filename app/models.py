"""Pydantic v2 request/response schemas (section 6 of spec.md)."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class DecisionIn(BaseModel):
    measure_id: str
    district_id: Optional[str] = None


class SimulateRequest(BaseModel):
    decisions: list[DecisionIn] = Field(default_factory=list)


class ScenarioRequest(BaseModel):
    team: str = Field(min_length=1, max_length=40)
    decisions: list[DecisionIn] = Field(default_factory=list)


class OptimizeRequest(BaseModel):
    decisions: list[DecisionIn] = Field(default_factory=list)
    locked_decisions: list[DecisionIn] = Field(default_factory=list, max_length=5)


class HealthOut(BaseModel):
    status: str
    ai_mode: str
