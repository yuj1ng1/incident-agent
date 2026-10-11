from collections.abc import Mapping

from services.configuration import SERVICES
from services.metric_profiles import METRIC_PROFILES
from services.profiles import ServiceProfile


class CapabilityUnavailable(LookupError):
    def __init__(self, service, reason):
        self.service = service
        self.reason = reason
        super().__init__(reason)


class ServiceRegistry:
    def __init__(self, definitions):
        profiles = {}
        entries = (
            definitions.items()
            if isinstance(definitions, Mapping)
            else ((None, v) for v in definitions)
        )
        for key, value in entries:
            profile = (
                value
                if isinstance(value, ServiceProfile)
                else ServiceProfile.model_validate(value)
            )
            if key is not None and key != profile.name:
                raise ValueError("service key/name mismatch")
            if profile.name in profiles:
                raise ValueError(f"duplicate service: {profile.name}")
            profiles[profile.name] = profile
        for profile in profiles.values():
            if len(set(profile.dependencies)) != len(profile.dependencies):
                raise ValueError(f"duplicate dependencies: {profile.name}")
            for name in profile.dependencies:
                if name not in profiles:
                    raise ValueError(f"unknown dependency: {name}")
                if name == profile.name:
                    raise ValueError(f"self dependency: {name}")
        self._profiles = profiles

    def get(self, service):
        profile = self._profiles.get(service)
        return profile.model_copy(deep=True) if profile else None

    def exists(self, service):
        return service in self._profiles

    def names(self):
        return tuple(self._profiles)

    def capabilities(self, service):
        profile = self.get(service)
        if profile is None:
            raise CapabilityUnavailable(service, "service_not_registered")
        return tuple(
            k
            for k in ("health", "logs", "metrics", "dependencies", "runtime", "source", "recovery")
            if k == "dependencies" or getattr(profile, k) is not None
        )

    def dependencies(self, service):
        self.require_capability(service, "dependencies")
        return self.get(service).dependencies

    def require_capability(self, service, capability):
        profile = self.get(service)
        if profile is None:
            raise CapabilityUnavailable(service, "service_not_registered")
        if capability not in self.capabilities(service):
            raise CapabilityUnavailable(
                service, f"{capability}_capability_not_configured"
            )
        return (
            profile.dependencies
            if capability == "dependencies"
            else getattr(profile, capability)
        )

    def describe(self, service):
        try:
            caps = self.capabilities(service)
        except CapabilityUnavailable as exc:
            return {
                "status": "no_data",
                "service": service,
                "reason": exc.reason,
            }
        profile = self.get(service)
        # Explicit allowlist: never serialize a Profile into an Agent response.
        result = {
            "status": "ok",
            "service": service,
            "capabilities": list(caps),
            "missing_capabilities": [
                k
                for k in ("health", "logs", "metrics", "runtime", "source")
                if k not in caps
            ],
            "providers": {
                k: getattr(profile, k).provider for k in caps if k != "dependencies"
            },
            "dependencies": list(profile.dependencies),
            "remediation": list(profile.remediation),
        }
        if profile.metrics:
            result.update(
                metric_profile=profile.metrics.profile,
                supported_metrics=list(METRIC_PROFILES[profile.metrics.profile]),
            )
        if profile.source and profile.source.investigation_paths:
            result["source_paths"] = list(profile.source.investigation_paths)
        return result


registry = ServiceRegistry(SERVICES)
