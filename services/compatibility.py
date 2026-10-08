"""Read-only, derived legacy views; modifications belong in SERVICES."""
from collections.abc import Mapping

from services.registry import registry


class LegacyView(Mapping):
    def __init__(self, field):
        self.field = field

    def projection(self, profile):
        if self.field == "SERVICE_URLS":
            return (
                profile.health.base_url
                if profile.health and profile.health.provider == "http"
                else None
            )
        if self.field == "SERVICE_DEPENDENCIES":
            return [
                {
                    "name": name,
                    "type": "database"
                    if registry.get(name).type == "postgresql"
                    else registry.get(name).type,
                }
                for name in profile.dependencies
            ]
        if self.field == "MONITORING_TARGETS":
            return (
                {
                    "type": "postgresql",
                    "job": profile.metrics.job,
                    "database": profile.metrics.database,
                }
                if profile.metrics and profile.metrics.profile == "postgresql"
                else None
            )
        if self.field == "CODE_REPOSITORIES":
            return profile.source.model_dump() if profile.source else None
        if profile.health and profile.health.provider == "postgresql":
            result = profile.health.model_dump(exclude={"provider"})
            return {
                "type": "database",
                "engine": "postgresql",
                **result,
            }
        return None

    def __getitem__(self, key):
        profile = registry.get(key)
        result = self.projection(profile) if profile else None
        if result is None:
            raise KeyError(key)
        return result

    def __iter__(self):
        return (
            name
            for name in registry.names()
            if self.projection(registry.get(name)) is not None
        )

    def __len__(self):
        return sum(1 for _ in self)
