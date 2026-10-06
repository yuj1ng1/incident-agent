import uuid
from models.incident import Incident

def create_incident_from_alert(alert):
    incident_id=f"inc-{uuid.uuid4().hex[:8]}"
    return Incident(
        incident_id=incident_id,
        service=alert.service,
        severity=alert.severity,
        status="investigating",
        alerts=[alert],
        evidence=[]
    )