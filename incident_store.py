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
            self.save(
                self.create_empty_data()
            )

    def create_empty_data(self):
        return {
            "version":2,
            "incidents":{},
            "active_by_fingerprint":{}
        }

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

        incident_id=data[
            "active_by_fingerprint"
        ].get(
            fingerprint
        )

        if incident_id is None:
            return None

        incident_data=data[
            "incidents"
        ].get(
            incident_id
        )

        if incident_data is None:
            return None

        if incident_data.get(
            "status"
        )=="resolved":
            return None

        return Incident.model_validate(
            incident_data
        )
    
    def save_incident(self,fingerprint,incident):
        data=self.load()

        incident.fingerprint=fingerprint

        data["incidents"][
            incident.incident_id
        ]=incident.model_dump(
            mode="json"
        )

        if incident.status=="resolved":
            current_incident_id=data[
                "active_by_fingerprint"
            ].get(
                fingerprint
            )

            if current_incident_id==incident.incident_id:
                data[
                    "active_by_fingerprint"
                ].pop(
                    fingerprint,
                    None
                )

        else:
            data[
                "active_by_fingerprint"
            ][fingerprint]=incident.incident_id

        self.save(data)

    def resolve_inactive_incidents(self,active_fingerprints):
        data=self.load()

        resolved_incidents=[]

        active_map=data[
            "active_by_fingerprint"
        ]

        for fingerprint,incident_id in list(
            active_map.items()
        ):
            if fingerprint in active_fingerprints:
                continue

            incident_data=data[
                "incidents"
            ].get(
                incident_id
            )

            if incident_data is None:
                active_map.pop(
                    fingerprint,
                    None
                )
                continue

            incident_data["status"]="resolved"

            resolved_incidents.append(
                incident_id
            )

            active_map.pop(
                fingerprint,
                None
            )

        if resolved_incidents:
            self.save(data)

        return resolved_incidents