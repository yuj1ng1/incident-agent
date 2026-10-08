from typing import Annotated, Literal
from urllib.parse import urlsplit
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from services.metric_profiles import METRIC_PROFILES


class Configuration(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )


class HTTPSettings(Configuration):
    base_url: str
    endpoint: str

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value):
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or any(c.isspace() or ord(c) < 32 for c in value)
        ):
            raise ValueError(
                "base_url must be an HTTP URL without credentials, query or fragment"
            )
        try:
            parsed.port
        except ValueError:
            raise ValueError("invalid URL port") from None
        return value.rstrip("/")

    @field_validator("endpoint")
    @classmethod
    def valid_endpoint(cls, value):
        if (
            not value.startswith("/")
            or value.startswith("//")
            or any(c in value for c in "?#\\")
            or any(ord(c) < 33 for c in value)
            or any(p in {".", ".."} for p in value.split("/"))
        ):
            raise ValueError("endpoint must be a local absolute URL path")
        return value


class HTTPHealth(HTTPSettings):
    provider: Literal["http"]
    endpoint: str = "/health"


class PostgreSQLHealth(Configuration):
    provider: Literal["postgresql"]
    host: str = Field(min_length=1)
    port: int = Field(
        default=5432,
        ge=1,
        le=65535,
    )
    database: str = Field(min_length=1)
    user: str = Field(min_length=1)
    password_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")


class LogsSettings(Configuration):
    provider: Literal["loki"]
    labels: dict[str, str]

    @field_validator("labels")
    @classmethod
    def valid_labels(cls, value):
        if (
            not value
            or any(
                not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", k)
                for k in value
            )
        ):
            raise ValueError("nonempty, valid Loki labels required")
        return value


class MetricsSettings(Configuration):
    provider: Literal["prometheus"]
    profile: str
    labels: dict[str, str] = Field(default_factory=dict)
    job: str | None = None
    database: str | None = None

    @model_validator(mode="after")
    def valid_profile(self):
        if self.profile not in METRIC_PROFILES:
            raise ValueError("unknown metric profile")
        if any(not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", k) for k in self.labels):
            raise ValueError("invalid metric label name")
        if self.profile == "postgresql":
            for key in ("job", "database"):
                value = getattr(self, key)
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"postgresql metrics require {key}")
        return self


class SourceSettings(Configuration):
    provider: Literal["github"]
    repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    branch: str = Field(min_length=1, max_length=200)
    investigation_paths: tuple[str, ...] = ()

    @field_validator("investigation_paths", mode="before")
    @classmethod
    def source_path_tuple(cls, value):
        return tuple(value) if isinstance(value, list) else value

    @field_validator("investigation_paths")
    @classmethod
    def safe_source_paths(cls, value):
        if any(
            not path
            or path.startswith("/")
            or "\\" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or any(ord(char) < 33 for char in path)
            for path in value
        ):
            raise ValueError("source investigation paths must be repository-relative")
        return value

    @field_validator("repo", "branch")
    @classmethod
    def valid_ref(cls, value):
        if (
            ".." in value
            or any(c.isspace() or ord(c) < 32 for c in value)
            or any(c in value for c in "\\%?#:")
        ):
            raise ValueError("invalid repository or branch")
        return value


class RuntimeSettings(HTTPSettings):
    provider: Literal["http_version"]
    endpoint: str = "/version"
    allowed_flags: tuple[str, ...] = ()

    @field_validator("allowed_flags", mode="before")
    @classmethod
    def flag_tuple(cls, value):
        return tuple(value) if isinstance(value, list) else value

    @field_validator("allowed_flags")
    @classmethod
    def safe_flags(cls, value):
        if (
            len(set(value)) != len(value)
            or any(
                not re.fullmatch(r"simulate_[a-z0-9_]+", k)
                for k in value
            )
        ):
            raise ValueError("runtime flags must be unique simulation flag names")
        return value


class ServiceProfile(Configuration):
    name: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
    type: str = Field(min_length=1)
    health: Annotated[
        HTTPHealth | PostgreSQLHealth, Field(discriminator="provider")
    ] | None = None
    logs: LogsSettings | None = None
    metrics: MetricsSettings | None = None
    source: SourceSettings | None = None
    runtime: RuntimeSettings | None = None
    dependencies: tuple[str, ...] = ()
    remediation: tuple[Literal["disable_demo_regression"], ...] = ()

    @field_validator(
        "dependencies",
        "remediation",
        mode="before",
    )
    @classmethod
    def config_tuple(cls, value):
        return tuple(value) if isinstance(value, list) else value
