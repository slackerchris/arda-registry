import yaml
from pathlib import Path
from typing import List

from app.models import ServiceRecord


def _probe_url(svc: ServiceRecord) -> str:
    check_type = svc.monitoring.type if svc.monitoring else "http"
    if check_type == "tcp":
        return f"{svc.network.ip}:{svc.backend.port}"
    if check_type == "ping":
        return svc.network.ip
    path = (svc.api and svc.api.status_path) or svc.backend.health_path or "/"
    if svc.api and svc.api.base_url:
        return f"{svc.api.base_url.rstrip('/')}{path}"
    if svc.network.primary_dns and svc.exposure.reverse_proxy:
        scheme = "https" if svc.exposure.force_ssl else "http"
        domain = svc.network.domain_for("health", "internal", "api") or svc.network.primary_dns
        return f"{scheme}://{domain}{path}"
    return f"{svc.backend.scheme}://{svc.network.ip}:{svc.backend.port}{path}"


def render_monitoring(
    services: List[ServiceRecord],
    output_path: str = "output/monitoring/targets.yml",
):
    """
    Render a Prometheus file_sd targets file for Blackbox Exporter.
    Includes services where monitoring.enabled=True or api.status_path is set.
    """
    monitored = [
        s for s in services
        if s.active and ((s.monitoring and s.monitoring.enabled) or (s.api and s.api.status_path))
    ]

    targets = [
        {
            "targets": [_probe_url(svc)],
            "labels": {
                "service": svc.name,
                "slug": svc.slug,
                "group": svc.group,
                "monitoring_type": svc.monitoring.type if svc.monitoring else "http",
            },
        }
        for svc in monitored
    ]

    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with open(output_file, "w") as f:
        yaml.dump(targets, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    print(f"Rendered monitoring targets to {output_path} ({len(targets)} services).")
