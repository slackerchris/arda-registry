from pathlib import Path
from typing import Dict, Literal, Optional
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field, field_validator


class ProxmoxNodeConfig(BaseModel):
    base_url: Optional[str] = None
    verify_ssl: Optional[bool] = None

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value):
        if not value:
            return value
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an http(s) URL")
        return value.rstrip("/")


class ProxmoxIntegrationConfig(BaseModel):
    key: Optional[str] = None
    base_url: Optional[str] = None
    verify_ssl: bool = False
    nodes: Dict[str, ProxmoxNodeConfig] = Field(default_factory=dict)

    @field_validator("key")
    @classmethod
    def require_env_key_reference(cls, value):
        if value and not value.startswith("$"):
            raise ValueError("proxmox.key must reference an environment variable like $PVE_API_TOKEN")
        return value

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value):
        if not value:
            return value
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an http(s) URL")
        return value.rstrip("/")


class MaflDeployConfig(BaseModel):
    mode: Literal["landing", "direct"] = "landing"
    node: Optional[str] = None
    container_id: Optional[int] = None
    nas_path: Optional[str] = None
    host: Optional[str] = None
    path: Optional[str] = None
    restart_command: Optional[str] = None


class MaflIntegrationConfig(BaseModel):
    source_path: str = "data/mafl.yml"
    output_path: str = "output/mafl/config.yml"
    deploy: MaflDeployConfig = Field(default_factory=MaflDeployConfig)


class IntegrationsConfig(BaseModel):
    proxmox: ProxmoxIntegrationConfig = Field(default_factory=ProxmoxIntegrationConfig)
    mafl: MaflIntegrationConfig = Field(default_factory=MaflIntegrationConfig)


def load_integrations(path: str | Path = "data/integrations.yml") -> IntegrationsConfig:
    config_path = Path(path)
    if not config_path.exists():
        return IntegrationsConfig()
    with open(config_path) as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        data = {}
    return IntegrationsConfig(**data)
