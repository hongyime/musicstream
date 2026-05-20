from typing import Generic, Optional, TypeVar, Any
from pydantic import BaseModel

DataT = TypeVar("DataT")

class ApiResponse(BaseModel, Generic[DataT]):
    data: Optional[DataT] = None
    error: Optional[str] = None
    meta: dict[str, Any] = {}

class TrackStats(BaseModel):
    total_tracks: int
    downloaded: int
    pending: int
    failed: int
    active: int
    progress_pct: float

class HealthStatus(BaseModel):
    service: str
    status: str
    latency_ms: Optional[float] = None
    updated_at: str
