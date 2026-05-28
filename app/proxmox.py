import os
from dataclasses import dataclass
from typing import Iterable, Literal
from urllib.parse import urlencode

import httpx

from app.integrations import ProxmoxIntegrationConfig, load_integrations
from app.models import ServiceRecord

LxcAction = Literal["start", "stop", "restart"]
VmAction = Literal["start", "stop", "restart"]
DockerAction = Literal["start", "stop", "restart"]


@dataclass(frozen=True)
class ProxmoxTarget:
    base_url: str
    node: str
    container_id: int
    token: str
    verify_ssl: bool


class ProxmoxError(RuntimeError):
    pass


def _resolve_secret(value: str | None) -> str:
    if not value:
        return ""
    if value.startswith("$"):
        return os.environ.get(value[1:], "").strip()
    return value.strip()


def _normalize_token(token: str) -> str:
    if token.startswith("PVEAPIToken="):
        return token
    return f"PVEAPIToken={token}"


def _service_matches_node(service: ServiceRecord, node: str) -> bool:
    wanted = node.strip().lower()
    return wanted in {
        service.slug.lower(),
        service.name.lower(),
    }


def _node_config(proxmox: ProxmoxIntegrationConfig, node: str):
    wanted = node.strip().lower()
    for name, config in proxmox.nodes.items():
        if name.strip().lower() == wanted:
            return config
    return None


def resolve_lxc_target(service: ServiceRecord, services: Iterable[ServiceRecord]) -> ProxmoxTarget:
    if not service.runtime or service.runtime.type not in {"lxc", "vm", "docker"} or not service.runtime.container_id:
        raise ProxmoxError("Service is not mapped to a Proxmox-managed container")

    try:
        integrations = load_integrations()
    except Exception as exc:
        raise ProxmoxError(f"Invalid integration config: {exc}") from exc
    proxmox = integrations.proxmox
    node = service.runtime.host.strip()
    node_service = next((candidate for candidate in services if _service_matches_node(candidate, node)), None)
    node_config = _node_config(proxmox, node)

    base_url = ""
    token = _resolve_secret(proxmox.key) or _resolve_secret(os.environ.get("PVE_API_TOKEN", ""))
    verify_ssl = proxmox.verify_ssl
    if node_config:
        if node_config.base_url:
            base_url = node_config.base_url.rstrip("/")
        if node_config.verify_ssl is not None:
            verify_ssl = node_config.verify_ssl

    if node_service:
        if not base_url and node_service.api and node_service.api.base_url:
            base_url = node_service.api.base_url.rstrip("/")
        elif not base_url:
            base_url = f"https://{node_service.network.ip}:8006"
        if not token and node_service.api and node_service.api.key:
            token = _resolve_secret(node_service.api.key)
        if not node_config and node_service.api:
            verify_ssl = node_service.api.verify_ssl

    if not base_url:
        base_url = proxmox.base_url or os.environ.get("PVE_URL", "").strip().rstrip("/") or f"https://{node}:8006"

    if not token:
        raise ProxmoxError("Missing Proxmox token. Set proxmox.key in data/integrations.yml or PVE_API_TOKEN.")

    return ProxmoxTarget(
        base_url=base_url,
        node=node,
        container_id=service.runtime.container_id,
        token=_normalize_token(token),
        verify_ssl=verify_ssl,
    )


def lxc_status_url(target: ProxmoxTarget, action: LxcAction) -> str:
    proxmox_action = "reboot" if action == "restart" else action
    return (
        f"{target.base_url}/api2/json/nodes/{target.node}"
        f"/lxc/{target.container_id}/status/{proxmox_action}"
    )


async def run_lxc_action(service: ServiceRecord, services: Iterable[ServiceRecord], action: LxcAction) -> str:
    if action not in {"start", "stop", "restart"}:
        raise ProxmoxError("Unsupported LXC action")

    target = resolve_lxc_target(service, services)
    async with httpx.AsyncClient(verify=target.verify_ssl) as client:
        try:
            response = await client.post(
                lxc_status_url(target, action),
                headers={"Authorization": target.token},
                timeout=15,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ProxmoxError(f"Proxmox {action} failed: {exc}") from exc

    payload = response.json()
    task_id = payload.get("data") if isinstance(payload, dict) else None
    return str(task_id or "")


async def get_power_status(service: ServiceRecord, services: list[ServiceRecord]) -> str:
    """Returns 'running', 'stopped', 'paused', or 'unknown'."""
    target = resolve_lxc_target(service, services)
    resource = "qemu" if service.runtime.type == "vm" else "lxc"
    url = (
        f"{target.base_url}/api2/json/nodes/{target.node}"
        f"/{resource}/{target.container_id}/status/current"
    )
    async with httpx.AsyncClient(verify=target.verify_ssl) as client:
        resp = await client.get(url, headers={"Authorization": target.token}, timeout=10)
        resp.raise_for_status()
    return resp.json().get("data", {}).get("status", "unknown")


def vm_status_url(target: ProxmoxTarget, action: VmAction) -> str:
    proxmox_action = "reboot" if action == "restart" else action
    return (
        f"{target.base_url}/api2/json/nodes/{target.node}"
        f"/qemu/{target.container_id}/status/{proxmox_action}"
    )


async def run_vm_action(service: ServiceRecord, services: Iterable[ServiceRecord], action: VmAction) -> str:
    if action not in {"start", "stop", "restart"}:
        raise ProxmoxError("Unsupported VM action")
    if not service.runtime or service.runtime.type != "vm":
        raise ProxmoxError("Service is not a QEMU VM")

    target = resolve_lxc_target(service, services)
    async with httpx.AsyncClient(verify=target.verify_ssl) as client:
        try:
            response = await client.post(
                vm_status_url(target, action),
                headers={"Authorization": target.token},
                timeout=15,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ProxmoxError(f"Proxmox VM {action} failed: {exc}") from exc

    payload = response.json()
    task_id = payload.get("data") if isinstance(payload, dict) else None
    return str(task_id or "")


async def run_docker_action(service: ServiceRecord, services: Iterable[ServiceRecord], action: DockerAction) -> str:
    if action not in {"start", "stop", "restart"}:
        raise ProxmoxError("Unsupported Docker action")
    if not service.runtime or service.runtime.type != "docker":
        raise ProxmoxError("Service is not a Docker container")
    if not service.runtime.container_name:
        raise ProxmoxError("runtime.container_name is not set")
    if not service.runtime.container_id:
        raise ProxmoxError("runtime.container_id (LXC vmid) is not set")

    target = resolve_lxc_target(service, services)
    exec_url = (
        f"{target.base_url}/api2/json/nodes/{target.node}"
        f"/lxc/{target.container_id}/exec"
    )
    command = ["docker", action, service.runtime.container_name]
    body = urlencode([("command", part) for part in command]).encode("utf-8")
    async with httpx.AsyncClient(verify=target.verify_ssl) as client:
        try:
            response = await client.post(
                exec_url,
                headers={
                    "Authorization": target.token,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                content=body,
                timeout=30,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ProxmoxError(f"Proxmox exec docker {action} failed: {exc}") from exc

    payload = response.json()
    task_id = payload.get("data") if isinstance(payload, dict) else None
    return str(task_id or "")
