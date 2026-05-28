import ipaddress
import re
from typing import Literal, List, Optional
from urllib.parse import urlparse
from pydantic import BaseModel, Field, field_validator

SLUG_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
SLUG_RE = re.compile(SLUG_PATTERN)


def _sanitize_domain_name(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw if "://" in raw else f"//{raw}")
    host = (parsed.netloc or parsed.path).strip()
    if "/" in host:
        host = host.split("/", 1)[0]
    if ":" in host and host.count(":") == 1:
        host = host.split(":", 1)[0]
    return host.strip().strip(".").lower()


class DomainNameConfig(BaseModel):
    name: str
    usage: List[str] = Field(default_factory=list)
    note: Optional[str] = None

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value):
        return _sanitize_domain_name(value)

    @field_validator("name")
    @classmethod
    def require_name(cls, value):
        if not value:
            raise ValueError("domain name is required")
        return value

    @field_validator("usage", mode="before")
    @classmethod
    def normalize_usage(cls, value):
        if value is None:
            return []
        if isinstance(value, str):
            return [part.strip().lower() for part in value.replace(",", "+").split("+") if part.strip()]
        if isinstance(value, list):
            return [str(item).strip().lower() for item in value if str(item).strip()]
        return []

class NetworkConfig(BaseModel):
    vlan: str
    ip: str
    dns: List[str] = Field(default_factory=list)
    domains: List[DomainNameConfig] = Field(default_factory=list)

    @field_validator("ip")
    @classmethod
    def validate_ip(cls, value):
        try:
            ipaddress.ip_address(value)
        except ValueError as exc:
            raise ValueError("network.ip must be a valid IP address") from exc
        return value

    @field_validator("dns", mode="before")
    @classmethod
    def normalize_dns(cls, value):
        if value is None:
            return []
        if isinstance(value, str):
            parts = [part.strip() for part in value.replace(",", ";").split(";")]
            return [_sanitize_domain_name(part) for part in parts if _sanitize_domain_name(part)]
        if isinstance(value, list):
            cleaned = []
            for item in value:
                sanitized = _sanitize_domain_name(str(item).strip())
                if sanitized:
                    cleaned.append(sanitized)
            return cleaned
        return []

    @field_validator("domains", mode="before")
    @classmethod
    def normalize_domains(cls, value):
        if value is None:
            return []
        if isinstance(value, list):
            normalized = []
            for item in value:
                if isinstance(item, dict):
                    normalized.append(item)
                elif isinstance(item, str) and item.strip():
                    normalized.append({"name": item.strip()})
            return normalized
        return []

    @property
    def all_dns(self) -> List[str]:
        ordered: list[str] = []
        seen: set[str] = set()
        for domain in self.domains:
            if domain.name not in seen:
                ordered.append(domain.name)
                seen.add(domain.name)
        for domain in self.dns:
            if domain not in seen:
                ordered.append(domain)
                seen.add(domain)
        return ordered

    @property
    def service_domains(self) -> List[str]:
        blocked = {"host", "node", "box", "machine"}
        structured = [
            domain.name
            for domain in self.domains
            if not blocked.intersection(domain.usage)
        ]
        if structured:
            legacy = [domain for domain in self.dns if domain not in structured]
            return structured + legacy
        return self.all_dns

    def domain_for(self, *usages: str) -> Optional[str]:
        wanted = {usage.strip().lower() for usage in usages if usage.strip()}
        if wanted:
            for domain in self.domains:
                if wanted.intersection(domain.usage):
                    return domain.name
        return self.primary_dns

    @property
    def primary_dns(self) -> Optional[str]:
        return self.service_domains[0] if self.service_domains else (self.all_dns[0] if self.all_dns else None)

    @property
    def dns_display(self) -> str:
        if self.domains:
            parts = []
            for domain in self.domains:
                if domain.usage:
                    parts.append(f"{'+'.join(domain.usage)}={domain.name}")
                else:
                    parts.append(domain.name)
            for domain in self.dns:
                if domain not in [d.name for d in self.domains]:
                    parts.append(domain)
            return "; ".join(parts)
        return "; ".join(self.dns)

class BackendConfig(BaseModel):
    scheme: Literal["http", "https"] = "http"
    port: int = Field(ge=1, le=65535)
    health_path: Optional[str] = None

    @field_validator("health_path")
    @classmethod
    def validate_health_path(cls, value):
        if value and not value.startswith("/"):
            raise ValueError("health_path must start with /")
        return value

class ExposureConfig(BaseModel):
    homepage: bool = False
    reverse_proxy: bool = False
    public: bool = False
    force_ssl: bool = True

class MonitoringConfig(BaseModel):
    enabled: bool = False
    type: Literal["http", "tcp", "ping"] = "http"
    maintenance: bool = False
    maintenance_reason: Optional[str] = None

class StorageMount(BaseModel):
    name: str
    path: str
    source: str

class StorageConfig(BaseModel):
    mounts: List[StorageMount] = Field(default_factory=list)

class RuntimeConfig(BaseModel):
    type: Literal["lxc", "vm", "docker", "bare"]
    host: str
    container_id: Optional[int] = Field(default=None, ge=1)
    container_name: Optional[str] = None
    gpu: bool = False

class BackupConfig(BaseModel):
    config: bool = True
    data: bool = False

class ApiConfig(BaseModel):
    base_url: Optional[str] = None
    key: Optional[str] = None
    key_header: str = "X-Api-Key"
    key_prefix: Optional[str] = None
    status_path: Optional[str] = None
    verify_ssl: bool = True

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value):
        if not value:
            return value
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an http(s) URL")
        return value.rstrip("/")

    @field_validator("key")
    @classmethod
    def require_env_key_reference(cls, value):
        if value and not value.startswith("$"):
            raise ValueError("api.key must reference an environment variable like $SERVICE_API_KEY")
        return value

    @field_validator("key_header")
    @classmethod
    def validate_key_header(cls, value):
        value = (value or "").strip()
        if not value:
            raise ValueError("api.key_header is required")
        if not re.fullmatch(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+", value):
            raise ValueError("api.key_header must be a valid HTTP header name")
        return value

    @field_validator("key_prefix")
    @classmethod
    def validate_key_prefix(cls, value):
        if value is None:
            return value
        value = value.strip()
        if not value:
            return None
        if "\r" in value or "\n" in value:
            raise ValueError("api.key_prefix cannot contain newlines")
        return value

    @field_validator("status_path")
    @classmethod
    def validate_status_path(cls, value):
        if value and not value.startswith("/"):
            raise ValueError("status_path must start with /")
        return value

class ServiceRecord(BaseModel):
    slug: str
    name: str
    app: Optional[str] = None
    group: str
    description: str
    tags: List[str] = Field(default_factory=list)
    network: NetworkConfig
    backend: BackendConfig
    exposure: ExposureConfig
    monitoring: Optional[MonitoringConfig] = None
    storage: Optional[StorageConfig] = None
    runtime: Optional[RuntimeConfig] = None
    backup: Optional[BackupConfig] = None
    api: Optional[ApiConfig] = None
    notes: Optional[str] = None
    active: bool = True

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, value):
        if not SLUG_RE.fullmatch(value or ""):
            raise ValueError("slug must use only letters, numbers, underscores, or hyphens")
        return value
