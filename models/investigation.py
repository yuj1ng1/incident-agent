from pydantic import BaseModel, Field
from enum import Enum
from datetime import datetime

class InvestigationStatus(str,Enum):
    COMPLETED="completed",
    MAX_ROUND_REACHED="max_round_reached"
    MODEL_INCOMPLETE="model_incomplete"

class InvestigationResult(BaseModel):
    success:bool
    status:InvestigationStatus
    answer:str
    model_rounds: int = 0
    tool_calls: int = 0
    tool_trace: list[dict] = Field(default_factory=list)

class InvestigationRecord(BaseModel):
    session_id: str | None = None
    investigated_at:datetime
    status:InvestigationStatus
    alert_value:float | None = None
    answer:str
    model_rounds: int = 0
    tool_calls: int = 0
    tool_trace: list[dict] = Field(default_factory=list)
