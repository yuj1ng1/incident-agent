from pydantic import BaseModel,Field,field_validator
from datetime import datetime
from models.investigation import InvestigationRecord

class Alert(BaseModel):#定义某个指标超过阈值
    service:str
    metric:str
    value:float
    threshold:float
    severity:str

class Evidence(BaseModel):
    source:str
    summary:str
    detail:str

class Incident(BaseModel):
    incident_id:str
    service:str
    severity:str
    status:str

    alerts:list[Alert]

    evidence:list[Evidence]=Field(default_factory=list)

    investigations:list[InvestigationRecord]=Field(
        default_factory=list
    )
    last_investigated_at:datetime|None=None
    last_investigated_value:float|None=None

    @field_validator(
        "evidence",
        "investigations",
        mode="before"
    )
    @classmethod
    def null_to_emptylist(cls,value):
        if value is None:
            return []
        return value