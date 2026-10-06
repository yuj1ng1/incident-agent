from pydantic import BaseModel
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

class InvestigationRecord(BaseModel):
    investigated_at:datetime
    status:InvestigationStatus
    alert_value:float
    answer:str
