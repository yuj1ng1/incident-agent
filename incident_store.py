import json
from pathlib import Path
from models.incident import Incident

class IncidentStore:
    def __init__(self,file_path):
        self.file_path=Path(file_path)
        self.file_path.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        if not self.file_path.exists():
            self.file_path.write_text(
                "{}",
                encoding="utf-8"
            )

    def load(self):
        content=self.file_path.read_text(
            encoding="utf-8"
        )
        return json.loads(content)

    def save(self,data):
        self.file_path.write_text(
            json.dumps(
                data,
                ensure_ascii=False,
                indent=2
            ),
            encoding="utf-8"
        )

    def get_active_incident(self,fingerprint):
        data=self.load()
        incident_data=data.get(fingerprint)

        if incident_data is None:
            return None
        if incident_data.get("status")=="resolved":
            return None
        return Incident.model_validate(incident_data)

    def save_incident(self,fingerprint,incident):
        data=self.load()
        data[fingerprint]=incident.model_dump(
            mode="json"
        )
        self.save(data)

    def resolve_inactive_incidents(self,active_fingerprints):
        data=self.load()
        resolved_incidents=[]

        for fingerprint,incidenta_data in data.items():
            if incidenta_data.get("status")=="resolved":
                continue
            if fingerprint in active_fingerprints:
                continue
            incidenta_data["status"]="resolved"
            resolved_incidents.append(incidenta_data["incident_id"])
        if resolved_incidents:
            self.save(data)
        return resolved_incidents