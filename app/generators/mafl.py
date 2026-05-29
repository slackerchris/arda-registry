from pathlib import Path
from datetime import datetime, timezone
from typing import List

import yaml

from app.integrations import load_integrations
from app.models import ServiceRecord


def _load_base_config(source_path: str) -> dict:
    path = Path(source_path)
    if not path.exists():
        return {
            "title": "Arda Registry",
            "lang": "en",
            "theme": "dark",
            "behaviour": {"target": "_blank"},
            "services": {},
        }
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("services", {})
    if not isinstance(data["services"], dict):
        data["services"] = {}
    return data


def _service_link(svc: ServiceRecord) -> str:
    if svc.network.primary_dns and svc.exposure.reverse_proxy:
        domain = svc.network.domain_for("dashboard", "public", "internal") or svc.network.primary_dns
        scheme = "https" if svc.exposure.force_ssl else "http"
        return f"{scheme}://{domain}/"
    return f"{svc.backend.scheme}://{svc.network.ip}:{svc.backend.port}/"


def _service_item(svc: ServiceRecord) -> dict:
    icon_name = svc.app or svc.slug
    if ":" not in icon_name:
        icon_name = f"mdi:{icon_name}"
    item = {
        "title": svc.name,
        "description": svc.description,
        "link": _service_link(svc),
        "icon": {
            "name": icon_name,
            "wrap": True,
        },
        "status": {"enabled": True},
    }
    if svc.tags:
        item["tags"] = [{"name": svc.tags[0].title(), "color": "blue"}]
    return item


def _merge_registry_services(config: dict, services: List[ServiceRecord]) -> int:
    rendered = 0
    groups = config.setdefault("services", {})
    for svc in services:
        if not svc.active or not svc.exposure.homepage:
            continue
        group = groups.setdefault(svc.group, [])
        item = _service_item(svc)
        for idx, existing in enumerate(group):
            if isinstance(existing, dict) and existing.get("title") == item["title"]:
                group[idx] = {**existing, **item}
                break
        else:
            group.append(item)
        rendered += 1
    return rendered


def _remove_empty_service_groups(config: dict) -> None:
    groups = config.get("services")
    if not isinstance(groups, dict):
        return
    for group_name, items in list(groups.items()):
        if isinstance(items, list) and not items:
            del groups[group_name]


def _write_rendered_config(config: dict, output_file: Path) -> None:
    try:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        with open(output_file, "w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    except PermissionError as exc:
        raise RuntimeError(
            "Cannot write Mafl config to "
            f"{output_file}. If MAFL_DEPLOY_MODE=direct, mount the Mafl config directory "
            "into the Arda container, for example: /docker/mafl:/docker/mafl. "
            "If it is already mounted, check that the Arda container user can write the existing config file."
        ) from exc


def render_mafl(
    services: List[ServiceRecord],
    output_path: str | None = None,
    source_path: str | None = None,
):
    integrations = load_integrations()
    mafl = integrations.mafl
    source = source_path or mafl.source_path
    output = output_path or mafl.output_path

    config = _load_base_config(source)
    rendered = _merge_registry_services(config, services)
    _remove_empty_service_groups(config)

    output_file = Path(output)
    _write_rendered_config(config, output_file)

    total = sum(len(items) for items in config.get("services", {}).values() if isinstance(items, list))
    print(f"Rendered Mafl config to {output} with {total} services.")
    if rendered:
        print(f"Updated {rendered} registry-managed dashboard service(s).")
    elif not any(s.active and s.exposure.homepage for s in services):
        candidates = [
            svc.slug
            for svc in services
            if svc.active and svc.exposure.reverse_proxy and not svc.exposure.homepage
        ]
        if candidates:
            print(
                "Note: no registry services have exposure.homepage=true. "
                f"Using {source} as the Mafl source config."
            )
    return output_file


def _mafl_output_path(mafl) -> str:
    if mafl.deploy.mode == "direct":
        return mafl.deploy.path or mafl.output_path
    return mafl.output_path


def _landing_zone_request_path(output_file: Path) -> Path:
    return output_file.with_name(f"{output_file.stem}.deploy.yml")


def _write_landing_zone_request(output_file: Path, deploy) -> Path:
    request_path = _landing_zone_request_path(output_file)
    payload = {
        "source": deploy.nas_path,
        "destination": deploy.path,
        "restart_command": deploy.restart_command,
        "rendered_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(request_path, "w") as f:
        yaml.dump(payload, f, default_flow_style=False, sort_keys=False)
    return request_path


async def sync_mafl(services: List[ServiceRecord]):
    integrations = load_integrations()
    mafl = integrations.mafl
    output_file = render_mafl(services, _mafl_output_path(mafl), mafl.source_path)
    deploy = mafl.deploy

    if deploy.mode == "direct":
        print(f"Mafl config rendered directly to {output_file}.")
        return

    if not deploy.path:
        print(f"Mafl config rendered to {output_file}. No container copy path configured.")
        return

    if deploy.nas_path and deploy.path:
        request_path = _write_landing_zone_request(output_file, deploy)
        print(
            "Mafl config rendered to landing zone. "
            f"LXC mover should copy {deploy.nas_path} to {deploy.path}. "
            f"Wrote deploy request {request_path}."
        )
        return

    print(
        f"Mafl config rendered to {output_file}. "
        "No NAS landing path configured; nothing else to deploy."
    )
