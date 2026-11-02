"""Pydantic models for the API and for the structured LLM output."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, HttpUrl, field_validator


class Category(str, Enum):
    billing = "Billing"
    technical = "Technical"
    account = "Account"
    sales = "Sales"
    feedback = "Feedback"
    general = "General"


class Urgency(str, Enum):
    low = "Low"
    medium = "Medium"
    high = "High"
    urgent = "Urgent"


class Sentiment(str, Enum):
    positive = "Positive"
    neutral = "Neutral"
    negative = "Negative"


class AnalysisResult(BaseModel):
    """The structured triage record the LLM must produce."""

    category: Category = Field(description="The primary category of the request.")
    urgency: Urgency = Field(description="How urgently support staff should respond.")
    sentiment: Sentiment = Field(description="The customer's overall tone.")
    summary: str = Field(
        min_length=1,
        max_length=400,
        description="A concise one-sentence summary of the issue, based strictly on the input text.",
    )
    action_item: str = Field(
        min_length=1,
        max_length=400,
        description="Recommended next step for support staff, based strictly on the input text.",
    )

    # Local models often vary casing ("high", "HIGH") — normalise before enum validation.
    @field_validator("category", "urgency", "sentiment", mode="before")
    @classmethod
    def _normalise_enum(cls, v: Any) -> Any:
        if isinstance(v, str):
            return v.strip().capitalize()
        return v

    @field_validator("summary", "action_item", mode="before")
    @classmethod
    def _strip(cls, v: Any) -> Any:
        return v.strip() if isinstance(v, str) else v


class AnalyzeRequest(BaseModel):
    text: str = Field(min_length=1, description="The input text or ticket content to analyze.")

    @field_validator("text")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("text must not be blank")
        return v


class JobCreate(AnalyzeRequest):
    callback_url: Optional[HttpUrl] = Field(
        default=None, description="Optional webhook; the finished job is POSTed here as JSON."
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict, description="Arbitrary caller data stored with the job (e.g. ticket id)."
    )


class BatchCreate(BaseModel):
    items: List[JobCreate] = Field(min_length=1)


class JobStatus(str, Enum):
    queued = "queued"
    processing = "processing"
    completed = "completed"
    failed = "failed"


class Job(BaseModel):
    id: str
    status: JobStatus
    text: str
    result: Optional[AnalysisResult] = None
    error: Optional[str] = None
    attempts: int = 0
    metadata: Dict[str, Any] = Field(default_factory=dict)
    callback_url: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime] = None
    duration_ms: Optional[int] = None


class JobList(BaseModel):
    items: List[Job]
    total: int
    limit: int
    offset: int


class Stats(BaseModel):
    total: int
    by_status: Dict[str, int]
    by_category: Dict[str, int]
    by_urgency: Dict[str, int]
    avg_duration_ms: Optional[float]
    queue_depth: int
    workers: int
