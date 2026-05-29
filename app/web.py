from pathlib import Path
from datetime import datetime, timezone
import io
import json
import zipfile
import asyncio
import logging
import base64
import ipaddress
import secrets
from urllib.parse import urlencode

from fastapi import FastAPI, Request, UploadFile, File
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

import os

import contextlib
import httpx

import yaml

from app.generators.ansible import render_ansible
from app.generators.mafl import render_mafl, sync_mafl
from app.generators.monitoring import render_monitoring
from app.integrations import load_integrations
from app.models import SLUG_RE, ServiceRecord
from app.proxmox import ProxmoxError, get_power_status, run_docker_action, run_lxc_action, run_vm_action
from app.registry import Registry

APP_VERSION = "0.1.17"

app = FastAPI(title="Arda Registry")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals["app_version"] = APP_VERSION

# Can be overridden by the CLI before uvicorn.run()
DATA_DIR = Path("data/services")
OPS_STATE_FILE = Path("output/state/ops_state.json")
OPS_CHECK_MIN_SECONDS = int(os.environ.get("OPS_CHECK_MIN_SECONDS", "300"))
OPS_FAIL_THRESHOLD = int(os.environ.get("OPS_FAIL_THRESHOLD", "2"))
OPS_RECOVERY_THRESHOLD = int(os.environ.get("OPS_RECOVERY_THRESHOLD", "2"))
OPS_CHECK_RETRIES = int(os.environ.get("OPS_CHECK_RETRIES", "2"))
OPS_CHECK_RETRY_DELAY_SECONDS = float(os.environ.get("OPS_CHECK_RETRY_DELAY_SECONDS", "0.35"))
OPS_CHECK_TIMEOUT_SECONDS = float(os.environ.get("OPS_CHECK_TIMEOUT_SECONDS", "5.0"))
OPS_LOG_FILE = Path("output/logs/ops_health.log")
CSRF_TOKEN = secrets.token_urlsafe(32)
POWER_STATUS_CACHE: dict = {"timestamp": None, "services": {}}

ops_logger = logging.getLogger("arda.ops.health")
app_logger = logging.getLogger("arda.app")


def _setup_ops_logger() -> None:
    if ops_logger.handlers:
        return
    OPS_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(OPS_LOG_FILE)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    ops_logger.addHandler(file_handler)
    ops_logger.setLevel(logging.INFO)
    ops_logger.propagate = False

    stderr_handler = logging.StreamHandler()
    stderr_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    app_logger.addHandler(stderr_handler)
    app_logger.setLevel(logging.INFO)
    app_logger.propagate = False


def _truthy_env(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _client_is_loopback(request: Request) -> bool:
    if not request.client:
        return False
    host = request.client.host
    if host in {"localhost", "testclient"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _host_header_is_loopback(request: Request) -> bool:
    host = request.url.hostname or ""
    if host in {"localhost", "testserver"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _authorized_by_header(request: Request) -> bool:
    token = os.environ.get("ARDA_UI_TOKEN", "").strip()
    password = os.environ.get("ARDA_UI_PASSWORD", "").strip()
    username = os.environ.get("ARDA_UI_USERNAME", "arda").strip() or "arda"
    auth = request.headers.get("authorization", "")

    if token and auth.startswith("Bearer "):
        return secrets.compare_digest(auth.removeprefix("Bearer ").strip(), token)

    if password and auth.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth.removeprefix("Basic ").strip()).decode()
            supplied_user, supplied_password = decoded.split(":", 1)
        except Exception:
            return False
        return secrets.compare_digest(supplied_user, username) and secrets.compare_digest(
            supplied_password,
            password,
        )

    return False


def _request_is_authorized(request: Request) -> bool:
    if _truthy_env("ARDA_AUTH_DISABLED"):
        return True
    if os.environ.get("ARDA_UI_TOKEN", "").strip() or os.environ.get("ARDA_UI_PASSWORD", "").strip():
        return _authorized_by_header(request)
    return _client_is_loopback(request) and _host_header_is_loopback(request)


@app.middleware("http")
async def require_ui_access(request: Request, call_next):
    if _request_is_authorized(request):
        return await call_next(request)
    if os.environ.get("ARDA_UI_TOKEN", "").strip() or os.environ.get("ARDA_UI_PASSWORD", "").strip():
        return PlainTextResponse(
            "Authentication required",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Arda Registry"'},
        )
    return PlainTextResponse(
        "Remote access requires ARDA_UI_PASSWORD or ARDA_UI_TOKEN",
        status_code=403,
    )


def _csrf_token() -> str:
    return os.environ.get("ARDA_CSRF_TOKEN", "").strip() or CSRF_TOKEN


def _csrf_context() -> dict:
    return {"csrf_token": _csrf_token()}


def _verify_csrf(form) -> bool:
    if _truthy_env("ARDA_CSRF_DISABLED"):
        return True
    supplied = str(form.get("csrf_token", "")).strip() or str(form.get("_csrf", "")).strip()
    return bool(supplied) and secrets.compare_digest(supplied, _csrf_token())


def _csrf_error_redirect(path: str = "/") -> RedirectResponse:
    return _redirect_with_query(path=path, error="Invalid or missing CSRF token")


def _redirect_with_query(path: str = "/", **params) -> RedirectResponse:
    clean = {key: value for key, value in params.items() if value is not None}
    query = urlencode(clean)
    return RedirectResponse(url=f"{path}?{query}" if query else path, status_code=303)


def _service_path(slug: str) -> Path:
    if not SLUG_RE.fullmatch(slug or ""):
        raise ValueError("Invalid service slug")
    root = DATA_DIR.resolve()
    path = (DATA_DIR / f"{slug}.yml").resolve()
    if root not in path.parents:
        raise ValueError("Service path escapes data directory")
    return path


def _runtime_lxc_key(service: ServiceRecord) -> str | None:
    if not service.runtime or service.runtime.type != "lxc" or not service.runtime.container_id:
        return None
    return f"{service.runtime.host}:{service.runtime.container_id}"


def _shared_lxc_runtimes(services: list[ServiceRecord]) -> set[str]:
    counts: dict[str, int] = {}
    for service in services:
        key = _runtime_lxc_key(service)
        if not key:
            continue
        counts[key] = counts.get(key, 0) + 1
    return {key for key, count in counts.items() if count > 1}


def _resolve_secret(value: str | None) -> str:
    if not value:
        return ""
    if value.startswith("$"):
        return os.environ.get(value[1:], "").strip()
    return value.strip()


def _format_api_key(key: str, prefix: str | None = None) -> str:
    key = key.strip()
    prefix = (prefix or "").strip()
    return f"{prefix} {key}" if prefix else key


def _normalize_pve_token(token: str) -> str:
    if token.startswith("PVEAPIToken="):
        return token
    return f"PVEAPIToken={token}"


def _is_proxmox_api_service(svc: ServiceRecord) -> bool:
    tags = {tag.lower() for tag in svc.tags}
    status_path = svc.api.status_path if svc.api else None
    health_path = svc.backend.health_path
    return "proxmox" in tags or any(
        path and path.startswith("/api2/json/")
        for path in (status_path, health_path)
    )


def _node_config_for_service(proxmox_config, svc: ServiceRecord):
    names = {
        svc.slug.lower(),
        svc.name.lower(),
        svc.network.ip.lower(),
        *(domain.lower() for domain in svc.network.all_dns),
    }
    if svc.api and svc.api.base_url:
        names.add(svc.api.base_url.rstrip("/").lower())
    for name, config in proxmox_config.nodes.items():
        aliases = {name.strip().lower()}
        if config.base_url:
            aliases.add(config.base_url.rstrip("/").lower())
        if aliases.intersection(names):
            return config
    return None


def _api_headers_for_service(svc: ServiceRecord) -> dict:
    headers: dict = {}
    if svc.api and svc.api.key:
        key = _resolve_secret(svc.api.key)
        if key:
            headers[svc.api.key_header] = _format_api_key(key, svc.api.key_prefix)

    if _is_proxmox_api_service(svc):
        try:
            proxmox = load_integrations().proxmox
            token = _resolve_secret(proxmox.key) or _resolve_secret(os.environ.get("PVE_API_TOKEN", ""))
        except Exception:
            token = _resolve_secret(os.environ.get("PVE_API_TOKEN", ""))
        if token:
            headers["Authorization"] = _normalize_pve_token(token)

    return headers


def _verify_ssl_for_service(svc: ServiceRecord) -> bool:
    verify_ssl = True if not svc.api else svc.api.verify_ssl
    if not _is_proxmox_api_service(svc):
        return verify_ssl
    try:
        proxmox = load_integrations().proxmox
        node_config = _node_config_for_service(proxmox, svc)
        if node_config and node_config.verify_ssl is not None:
            return node_config.verify_ssl
        return proxmox.verify_ssl
    except Exception:
        return verify_ssl


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seconds_since_iso(ts: str | None) -> float:
    if not ts:
        return float("inf")
    try:
        dt = datetime.fromisoformat(ts)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception:
        return float("inf")


def _load_ops_state() -> dict:
    if not OPS_STATE_FILE.exists():
        return {"services": {}, "history": []}
    try:
        with open(OPS_STATE_FILE) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"services": {}, "history": []}
        data.setdefault("services", {})
        data.setdefault("history", [])
        return data
    except Exception:
        return {"services": {}, "history": []}


def _save_ops_state(state: dict) -> None:
    OPS_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OPS_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def _cached_service_statuses(checkable: list[ServiceRecord], services_state: dict, latest: dict | None) -> dict:
    cached: dict = {}
    stale = latest is None or _seconds_since_iso(latest.get("timestamp")) >= OPS_CHECK_MIN_SECONDS
    for svc in checkable:
        previous = services_state.get(svc.slug, {})
        status_text = previous.get("status", "unknown")
        cached[svc.slug] = {
            "up": status_text == "up",
            "status": status_text,
            "status_code": previous.get("status_code"),
            "error": previous.get("error"),
            "maintenance": bool(previous.get("maintenance")),
            "maintenance_reason": previous.get("maintenance_reason") or "",
            "up_since": previous.get("up_since"),
            "last_checked": previous.get("last_checked"),
            "last_down": previous.get("last_down"),
            "cached": True,
            "stale": stale,
        }
    return cached


def _load_registry() -> Registry:
    registry = Registry(str(DATA_DIR))
    registry.load()
    return registry


def _load_valid_registry() -> Registry:
    registry = _load_registry()
    registry.validate_cross_service()
    if registry.errors:
        error_text = "; ".join(f"{path}: {err}" for path, err in registry.errors[:3])
        raise ValueError(error_text)
    return registry


def _health_log(event: str, **fields) -> None:
    payload = {"event": event, **fields}
    try:
        ops_logger.info(json.dumps(payload, sort_keys=True))
    except Exception:
        # Logging should never break status checks.
        pass


def _parse_domain_entries(raw_value: str) -> tuple[list[str], list[dict]]:
    plain_dns: list[str] = []
    structured: list[dict] = []
    for token in [part.strip() for part in raw_value.replace(",", ";").split(";") if part.strip()]:
        if "=" in token:
            usage_text, name = token.split("=", 1)
            name = name.strip()
            usages = [part.strip().lower() for part in usage_text.split("+") if part.strip()]
            if name:
                structured.append({"name": name, "usage": usages})
        else:
            plain_dns.append(token)
    return plain_dns, structured


def _normalize_domain_entries(entries_raw) -> tuple[list[str], list[dict], list[dict]]:
    plain_dns: list[str] = []
    structured_domains: list[dict] = []
    form_entries: list[dict] = []
    if not isinstance(entries_raw, list):
        return plain_dns, structured_domains, form_entries

    for item in entries_raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        usage_raw = item.get("usage", [])
        note = str(item.get("note", "")).strip()
        if isinstance(usage_raw, str):
            usage = [part.strip().lower() for part in usage_raw.replace(",", "+").split("+") if part.strip()]
        elif isinstance(usage_raw, list):
            usage = [str(part).strip().lower() for part in usage_raw if str(part).strip()]
        else:
            usage = []
        if not name:
            continue
        form_entry = {
            "name": name,
            "usage": "+".join(usage),
            "note": note,
        }
        form_entries.append(form_entry)
        if usage or note:
            domain_entry = {"name": name}
            if usage:
                domain_entry["usage"] = usage
            if note:
                domain_entry["note"] = note
            structured_domains.append(domain_entry)
        else:
            plain_dns.append(name)
    return plain_dns, structured_domains, form_entries


def _parse_form(fd: dict) -> dict:
    """Convert flat form fields to a nested service data dict."""
    tags = [t.strip() for t in fd.get("tags", "").split(",") if t.strip()]
    data: dict = {
        "slug": fd.get("slug", "").strip(),
        "name": fd.get("name", "").strip(),
        "app": fd.get("app", "").strip() or None,
        "group": fd.get("group", "").strip(),
        "description": fd.get("description", "").strip(),
        "tags": tags,
        "network": {
            "vlan": fd.get("network_vlan", "").strip(),
            "ip": fd.get("network_ip", "").strip(),
        },
        "backend": {
            "scheme": fd.get("backend_scheme", "http"),
            "port": fd.get("backend_port") or 0,
        },
        "exposure": {
            "homepage": "exposure_homepage" in fd,
            "reverse_proxy": "exposure_reverse_proxy" in fd,
            "public": "exposure_public" in fd,
            "force_ssl": "exposure_force_ssl" in fd,
        },
    }
    entries_json = fd.get("network_domains_json", "").strip()
    if entries_json:
        try:
            parsed_entries = json.loads(entries_json)
        except Exception:
            parsed_entries = []
        plain_dns, structured_domains, _ = _normalize_domain_entries(parsed_entries)
        if plain_dns:
            data["network"]["dns"] = plain_dns
        if structured_domains:
            data["network"]["domains"] = structured_domains
    elif dns := fd.get("network_dns", "").strip():
        plain_dns, structured_domains = _parse_domain_entries(dns)
        if plain_dns:
            data["network"]["dns"] = plain_dns
        if structured_domains:
            data["network"]["domains"] = structured_domains
    if health := fd.get("backend_health_path", "").strip():
        data["backend"]["health_path"] = health
    monitoring_enabled = "monitoring_enabled" in fd
    monitoring_maintenance = "monitoring_maintenance" in fd
    monitoring_reason = fd.get("monitoring_maintenance_reason", "").strip()
    if monitoring_enabled or monitoring_maintenance or monitoring_reason:
        data["monitoring"] = {
            "enabled": monitoring_enabled,
            "type": fd.get("monitoring_type", "http"),
            "maintenance": monitoring_maintenance,
        }
        if monitoring_reason:
            data["monitoring"]["maintenance_reason"] = monitoring_reason
    runtime_type = fd.get("runtime_type", "").strip()
    runtime_host = fd.get("runtime_host", "").strip()
    if runtime_type and runtime_host:
        data["runtime"] = {"type": runtime_type, "host": runtime_host, "gpu": "runtime_gpu" in fd}
        if cid := fd.get("runtime_container_id", "").strip():
            data["runtime"]["container_id"] = cid
        if cname := fd.get("runtime_container_name", "").strip():
            data["runtime"]["container_name"] = cname
    backup_config = "backup_config" in fd
    backup_data = "backup_data" in fd
    # Omit the block when at the default state (config=True, data=False); the Pydantic default applies.
    if not backup_config or backup_data:
        data["backup"] = {"config": backup_config, "data": backup_data}
    if notes := fd.get("notes", "").strip():
        data["notes"] = notes
    api_key = fd.get("api_key", "").strip()
    api_base_url = fd.get("api_base_url", "").strip()
    api_status_path = fd.get("api_status_path", "").strip()
    api_key_header = fd.get("api_key_header", "X-Api-Key").strip() or "X-Api-Key"
    api_key_prefix = fd.get("api_key_prefix", "").strip()
    api_verify_ssl = "api_verify_ssl" in fd
    if api_key or api_base_url or api_status_path:
        data["api"] = {
            "base_url": api_base_url or None,
            "key": api_key or None,
            "key_header": api_key_header,
            "key_prefix": api_key_prefix or None,
            "status_path": api_status_path or None,
            "verify_ssl": api_verify_ssl,
        }
    return data


def _service_to_form_data(svc: ServiceRecord) -> dict:
    """Flatten a ServiceRecord into form field names for template repopulation."""
    fd: dict = {
        "slug": svc.slug,
        "name": svc.name,
        "app": svc.app or "",
        "group": svc.group,
        "description": svc.description,
        "tags": ", ".join(svc.tags),
        "network_vlan": svc.network.vlan,
        "network_ip": svc.network.ip,
        "network_dns": svc.network.dns_display,
        "network_domain_entries": [],
        "network_domains_json": "[]",
        "backend_scheme": svc.backend.scheme,
        "backend_port": str(svc.backend.port),
        "backend_health_path": svc.backend.health_path or "",
        "notes": svc.notes or "",
    }
    if svc.exposure.homepage:
        fd["exposure_homepage"] = "on"
    if svc.exposure.reverse_proxy:
        fd["exposure_reverse_proxy"] = "on"
    if svc.exposure.public:
        fd["exposure_public"] = "on"
    if svc.exposure.force_ssl:
        fd["exposure_force_ssl"] = "on"
    if svc.monitoring:
        if svc.monitoring.enabled:
            fd["monitoring_enabled"] = "on"
        fd["monitoring_type"] = svc.monitoring.type
        if svc.monitoring.maintenance:
            fd["monitoring_maintenance"] = "on"
        if svc.monitoring.maintenance_reason:
            fd["monitoring_maintenance_reason"] = svc.monitoring.maintenance_reason
    if svc.runtime:
        fd["runtime_type"] = svc.runtime.type
        fd["runtime_host"] = svc.runtime.host
        if svc.runtime.container_id:
            fd["runtime_container_id"] = str(svc.runtime.container_id)
        if svc.runtime.container_name:
            fd["runtime_container_name"] = svc.runtime.container_name
        if svc.runtime.gpu:
            fd["runtime_gpu"] = "on"
    backup = svc.backup
    if backup is None or backup.config:
        fd["backup_config"] = "on"
    if backup and backup.data:
        fd["backup_data"] = "on"
    if svc.api:
        fd["api_base_url"] = svc.api.base_url or ""
        fd["api_key"] = svc.api.key or ""
        fd["api_key_header"] = svc.api.key_header if svc.api.key else ""
        fd["api_key_prefix"] = svc.api.key_prefix or ""
        fd["api_status_path"] = svc.api.status_path or ""
        if svc.api.verify_ssl:
            fd["api_verify_ssl"] = "on"

    domain_entries = []
    structured_names = set()
    for domain in svc.network.domains:
        structured_names.add(domain.name)
        domain_entries.append({
            "name": domain.name,
            "usage": "+".join(domain.usage),
            "note": domain.note or "",
        })
    for name in svc.network.dns:
        if name not in structured_names:
            domain_entries.append({"name": name, "usage": "", "note": ""})
    fd["network_domain_entries"] = domain_entries
    fd["network_domains_json"] = json.dumps(domain_entries)
    return fd


def _suggest_duplicate_slug(source_slug: str) -> str:
    base = f"{source_slug}-copy"
    candidate = base
    suffix = 2
    while _service_path(candidate).exists() and suffix <= 99:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _augment_form_with_domain_entries(fd: dict) -> dict:
    try:
        parsed_entries = json.loads(fd.get("network_domains_json", "[]") or "[]")
    except Exception:
        parsed_entries = []
    _, _, form_entries = _normalize_domain_entries(parsed_entries)
    fd["network_domain_entries"] = form_entries
    fd["network_domains_json"] = json.dumps(form_entries)
    return fd


def _read_log_entries(limit: int = 500) -> list[dict]:
    if not OPS_LOG_FILE.exists():
        return []
    entries: list[dict] = []
    try:
        lines = OPS_LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                date_part, time_part, _level, message = line.split(" ", 3)
                payload = json.loads(message)
                payload["_ts"] = f"{date_part} {time_part}"
                payload["_level"] = _level
                entries.append(payload)
            except Exception:
                continue
            if len(entries) >= limit:
                break
    except Exception:
        pass
    return entries


# ---------------------------------------------------------------------------
# Index — service list
# ---------------------------------------------------------------------------


async def _check_health(svc: ServiceRecord, client: httpx.AsyncClient) -> dict:
    check_type = svc.monitoring.type if svc.monitoring else "http"
    if check_type == "tcp":
        target = f"{svc.network.ip}:{svc.backend.port}"
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(svc.network.ip, svc.backend.port),
                timeout=OPS_CHECK_TIMEOUT_SECONDS,
            )
            writer.close()
            await writer.wait_closed()
            return {"up": True, "status_code": None, "error": None, "attempts": 1, "url": target}
        except Exception as e:
            return {
                "up": False,
                "status_code": None,
                "error": str(e)[:120],
                "attempts": 1,
                "url": target,
            }

    if check_type == "ping":
        try:
            proc = await asyncio.create_subprocess_exec(
                "ping",
                "-c",
                "1",
                "-W",
                "2",
                svc.network.ip,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=OPS_CHECK_TIMEOUT_SECONDS)
            return {
                "up": proc.returncode == 0,
                "status_code": None,
                "error": None if proc.returncode == 0 else (stderr.decode(errors="ignore")[:120] or "Ping failed"),
                "attempts": 1,
                "url": svc.network.ip,
            }
        except Exception as e:
            return {
                "up": False,
                "status_code": None,
                "error": str(e)[:120],
                "attempts": 1,
                "url": svc.network.ip,
            }

    path = (svc.api and svc.api.status_path) or svc.backend.health_path or "/"
    if svc.api and svc.api.base_url:
        url = f"{svc.api.base_url.rstrip('/')}{path}"
    elif svc.network.primary_dns and svc.exposure.reverse_proxy:
        scheme = "https" if svc.exposure.force_ssl else "http"
        domain = (
            svc.network.domain_for("health", "internal", "api")
            or svc.network.domain_for("public", "external")
            or svc.network.primary_dns
        )
        url = f"{scheme}://{domain}{path}"
    else:
        url = f"{svc.backend.scheme}://{svc.network.ip}:{svc.backend.port}{path}"

    headers = _api_headers_for_service(svc)

    retries = max(1, OPS_CHECK_RETRIES)
    verify_ssl = _verify_ssl_for_service(svc)
    last_error = None
    last_status = None
    # Use the shared client for verified connections; open a dedicated one for self-signed certs.
    ctx = contextlib.nullcontext(client) if verify_ssl else httpx.AsyncClient(verify=False, follow_redirects=True)
    async with ctx as http_client:
        for attempt in range(1, retries + 1):
            try:
                resp = await http_client.get(url, headers=headers, timeout=OPS_CHECK_TIMEOUT_SECONDS, follow_redirects=True)
                auth_failed = resp.status_code in {401, 403}
                is_up = resp.status_code < 500 and not auth_failed
                last_status = resp.status_code
                if is_up or attempt == retries:
                    return {
                        "up": is_up,
                        "status_code": resp.status_code,
                        "error": None if is_up else f"HTTP {resp.status_code}",
                        "attempts": attempt,
                        "url": url,
                    }
            except Exception as e:
                last_error = (str(e) or type(e).__name__)[:120]
                if attempt == retries:
                    break

            await asyncio.sleep(max(0.0, OPS_CHECK_RETRY_DELAY_SECONDS))

    return {
        "up": False,
        "status_code": last_status,
        "error": last_error or "Health check failed",
        "attempts": retries,
        "url": url,
    }


async def _fetch_unifi_summary() -> dict:
    registry = _load_registry()
    unifi_svc = next((s for s in registry.services if s.slug.lower() == "unifi"), None)

    svc_url = ""
    svc_token = ""
    svc_token_header = "X-API-KEY"
    if unifi_svc:
        if unifi_svc.api and unifi_svc.api.base_url:
            svc_url = unifi_svc.api.base_url.strip().rstrip("/")
        else:
            svc_url = f"{unifi_svc.backend.scheme}://{unifi_svc.network.ip}:{unifi_svc.backend.port}".rstrip("/")
        if unifi_svc.api and unifi_svc.api.key:
            svc_token = _format_api_key(
                _resolve_secret(unifi_svc.api.key),
                unifi_svc.api.key_prefix,
            )
        if unifi_svc.api and unifi_svc.api.key_header:
            svc_token_header = unifi_svc.api.key_header

    unifi_url = (os.environ.get("UNIFI_URL", "").strip().rstrip("/") or svc_url)
    api_token = (
        os.environ.get("UNIFI_API_TOKEN", "").strip()
        or os.environ.get("UNIFI_API_KEY", "").strip()
        or svc_token
    )
    token_header = os.environ.get("UNIFI_API_HEADER", "").strip() or svc_token_header
    username = os.environ.get("UNIFI_USERNAME", "").strip()
    password = os.environ.get("UNIFI_PASSWORD", "").strip()
    site = os.environ.get("UNIFI_SITE", "default").strip() or "default"
    verify_ssl = os.environ.get("UNIFI_VERIFY_SSL", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    if not unifi_url:
        return {
            "configured": False,
            "up": False,
            "error": "Missing UNIFI_URL",
        }
    if not api_token and (not username or not password):
        return {
            "configured": False,
            "up": False,
            "error": "Set UNIFI_API_TOKEN or UNIFI_USERNAME/UNIFI_PASSWORD",
        }

    async def _get_data(client: httpx.AsyncClient, path: str) -> list:
        resp = await client.get(f"{unifi_url}{path}", timeout=10.0)
        resp.raise_for_status()
        payload = resp.json()
        if isinstance(payload, dict):
            return payload.get("data", [])
        return []

    try:
        async with httpx.AsyncClient(
            verify=verify_ssl,
            follow_redirects=True,
        ) as client:
            # UniFi OS path first, then legacy controller path fallback.
            base_paths = [f"/proxy/network/api/s/{site}", f"/api/s/{site}"]

            # Prefer API token auth if provided.
            if api_token:
                client.headers.update({token_header: api_token})

            login_error = None
            if not api_token:
                try:
                    login_resp = await client.post(
                        f"{unifi_url}/api/auth/login",
                        json={"username": username, "password": password, "remember": True},
                        timeout=10.0,
                    )
                    login_resp.raise_for_status()
                except Exception as e:
                    login_error = e

            last_error = None
            for base_path in base_paths:
                try:
                    health = await _get_data(client, f"{base_path}/stat/health")
                    devices = await _get_data(client, f"{base_path}/stat/device")
                    clients = await _get_data(client, f"{base_path}/stat/sta")
                    wan_items = [
                        h
                        for h in health
                        if h.get("subsystem") in {"wan", "www", "internet"}
                    ]
                    wan_down = len([w for w in wan_items if w.get("status") != "ok"])
                    devices_down = len([d for d in devices if d.get("state") != 1])
                    return {
                        "configured": True,
                        "up": True,
                        "site": site,
                        "wan_down": wan_down,
                        "devices_total": len(devices),
                        "devices_down": devices_down,
                        "clients": len(clients),
                        "error": None,
                    }
                except Exception as e:
                    last_error = e
            # If token auth failed and creds exist, try session login as fallback.
            if api_token and username and password:
                try:
                    client.headers.pop(token_header, None)
                    login_resp = await client.post(
                        f"{unifi_url}/api/auth/login",
                        json={"username": username, "password": password, "remember": True},
                        timeout=10.0,
                    )
                    login_resp.raise_for_status()
                    for base_path in base_paths:
                        try:
                            health = await _get_data(client, f"{base_path}/stat/health")
                            devices = await _get_data(client, f"{base_path}/stat/device")
                            clients = await _get_data(client, f"{base_path}/stat/sta")
                            wan_items = [
                                h
                                for h in health
                                if h.get("subsystem") in {"wan", "www", "internet"}
                            ]
                            wan_down = len([w for w in wan_items if w.get("status") != "ok"])
                            devices_down = len([d for d in devices if d.get("state") != 1])
                            return {
                                "configured": True,
                                "up": True,
                                "site": site,
                                "wan_down": wan_down,
                                "devices_total": len(devices),
                                "devices_down": devices_down,
                                "clients": len(clients),
                                "error": None,
                            }
                        except Exception as e:
                            last_error = e
                except Exception as e:
                    login_error = e
            if login_error and not last_error:
                raise login_error
            raise last_error if last_error else RuntimeError("Unknown UniFi API error")
    except Exception as e:
        return {
            "configured": True,
            "up": False,
            "error": str(e)[:140],
        }


@app.get("/api/status")
async def service_status(request: Request):
    registry = _load_registry()
    checkable = [
        s for s in registry.services
        if s.active and ((s.monitoring and s.monitoring.enabled) or s.api)
    ]
    if not checkable:
        return {}

    state = _load_ops_state()
    services_state = state.get("services", {})
    history = state.get("history", [])
    latest = history[-1] if history else None
    force_refresh = request.query_params.get("refresh") == "1"

    # The index page should stay snappy: serve cached or unknown statuses unless
    # the caller explicitly asks for live checks.
    if not force_refresh:
        cached = _cached_service_statuses(checkable, services_state, latest)
        _health_log(
            "status_cache_hit",
            checked=len(checkable),
            force_refresh=force_refresh,
            cache_age_seconds=round(_seconds_since_iso(latest.get("timestamp")), 2) if latest else None,
        )
        return cached

    now = _now_iso()

    maintenance_slugs = {
        s.slug: (s.monitoring.maintenance_reason if s.monitoring else None)
        for s in checkable
        if s.monitoring and s.monitoring.maintenance
    }
    to_check = [s for s in checkable if s.slug not in maintenance_slugs]

    results_by_slug: dict = {}
    if to_check:
        async with httpx.AsyncClient() as client:
            results = await asyncio.gather(
                *[_check_health(s, client) for s in to_check],
                return_exceptions=True,
            )
        results_by_slug.update({
            svc.slug: (r if isinstance(r, dict) else {"up": False, "status_code": None, "error": str(r)})
            for svc, r in zip(to_check, results)
        })

    for slug, reason in maintenance_slugs.items():
        results_by_slug[slug] = {
            "up": False,
            "status_code": None,
            "error": None,
            "maintenance": True,
            "maintenance_reason": reason or "Maintenance mode",
        }

    down_services: list[str] = []
    maintenance_services: list[str] = []
    up_count = 0

    for svc in checkable:
        slug = svc.slug
        result = results_by_slug.get(slug, {"up": False, "status_code": None, "error": "No result"})
        previous = services_state.get(slug, {})
        is_maintenance = bool(result.get("maintenance"))
        previous_status = previous.get("status")

        prev_failures = int(previous.get("consecutive_failures", 0) or 0)
        prev_successes = int(previous.get("consecutive_successes", 0) or 0)

        consecutive_failures = prev_failures
        consecutive_successes = prev_successes

        if is_maintenance:
            status_text = "maintenance"
            maintenance_services.append(slug)
        else:
            raw_up = bool(result.get("up"))
            if raw_up:
                consecutive_successes = prev_successes + 1
                consecutive_failures = 0
            else:
                consecutive_failures = prev_failures + 1
                consecutive_successes = 0

            if previous_status == "up" and not raw_up:
                status_text = "down" if consecutive_failures >= max(1, OPS_FAIL_THRESHOLD) else "up"
            elif previous_status == "down" and raw_up:
                status_text = "up" if consecutive_successes >= max(1, OPS_RECOVERY_THRESHOLD) else "down"
            else:
                status_text = "up" if raw_up else "down"

            if status_text == "up":
                up_count += 1
            else:
                down_services.append(slug)

        updated = {
            "status": status_text,
            "last_checked": now,
            "status_code": result.get("status_code"),
            "error": result.get("error"),
            "maintenance": is_maintenance,
            "maintenance_reason": result.get("maintenance_reason") or "",
            "up_since": previous.get("up_since"),
            "last_down": previous.get("last_down"),
            "consecutive_failures": 0 if is_maintenance else consecutive_failures,
            "consecutive_successes": 0 if is_maintenance else consecutive_successes,
        }

        if status_text == "up":
            if previous.get("status") != "up":
                updated["up_since"] = now
        elif status_text == "down":
            if previous.get("status") != "down":
                updated["last_down"] = now

        services_state[slug] = updated
        result["up_since"] = updated.get("up_since")
        result["last_checked"] = updated.get("last_checked")
        result["last_down"] = updated.get("last_down")
        result["status"] = status_text

        _health_log(
            "service_check",
            slug=slug,
            raw_up=bool(result.get("up")) and not is_maintenance,
            effective_status=status_text,
            previous_status=previous_status,
            status_code=result.get("status_code"),
            error=result.get("error"),
            attempts=result.get("attempts"),
            url=result.get("url"),
            consecutive_failures=updated.get("consecutive_failures"),
            consecutive_successes=updated.get("consecutive_successes"),
            maintenance=is_maintenance,
        )

    state["services"] = services_state

    color = "red" if down_services else ("yellow" if maintenance_services else "green")
    summary = {
        "timestamp": now,
        "color": color,
        "checked": len(checkable),
        "up": up_count,
        "down": len(down_services),
        "maintenance": len(maintenance_services),
        "down_list": down_services,
    }
    history.append(summary)
    state["history"] = history[-60:]
    _save_ops_state(state)

    _health_log(
        "status_summary",
        color=color,
        checked=summary["checked"],
        up=summary["up"],
        down=summary["down"],
        maintenance=summary["maintenance"],
        down_list=down_services,
        thresholds={
            "fail": max(1, OPS_FAIL_THRESHOLD),
            "recovery": max(1, OPS_RECOVERY_THRESHOLD),
            "retries": max(1, OPS_CHECK_RETRIES),
        },
    )

    return results_by_slug


@app.get("/api/ops/summary")
async def ops_summary():
    state = _load_ops_state()
    history = state.get("history", [])
    latest = history[-1] if history else {
        "timestamp": None,
        "color": "gray",
        "checked": 0,
        "up": 0,
        "down": 0,
        "maintenance": 0,
        "down_list": [],
    }
    return {
        "latest": latest,
        "recent": list(reversed(history[-20:])),
        "services": state.get("services", {}),
    }


@app.get("/api/unifi/summary")
async def unifi_summary():
    return await _fetch_unifi_summary()


@app.get("/api/proxmox/status")
async def proxmox_power_status(request: Request):
    registry = _load_registry()
    checkable = [
        s for s in registry.services
        if s.active and s.runtime and s.runtime.type in {"lxc", "vm", "docker"} and s.runtime.container_id
    ]
    if not checkable:
        return {}
    force_refresh = request.query_params.get("refresh") == "1"
    if not force_refresh:
        cached_services = POWER_STATUS_CACHE.get("services", {})
        return {
            svc.slug: cached_services.get(svc.slug, {"power": "unknown", "cached": True})
            for svc in checkable
        }
    services_list = list(registry.services)

    async def _check(svc):
        try:
            power = await get_power_status(svc, services_list)
            return svc.slug, {"power": power}
        except Exception as exc:
            return svc.slug, {"power": "unknown", "error": str(exc)[:80]}

    results = dict(await asyncio.gather(*[_check(s) for s in checkable]))
    POWER_STATUS_CACHE["timestamp"] = _now_iso()
    POWER_STATUS_CACHE["services"] = results
    return results

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    registry = _load_registry()
    active_services = [s for s in registry.services if s.active]
    inactive_services = [s for s in registry.services if not s.active]
    active_services.sort(key=lambda s: (s.group.lower(), s.name.lower()))
    inactive_services.sort(key=lambda s: (s.group.lower(), s.name.lower()))
    inactive_count = len(inactive_services)
    shared_lxc_runtimes = _shared_lxc_runtimes(registry.services)
    return templates.TemplateResponse(request, "index.html", {
        "active_services": active_services,
        "inactive_services": inactive_services,
        "inactive_count": inactive_count,
        "shared_lxc_runtimes": shared_lxc_runtimes,
        "load_errors": registry.errors,
        "flash_error": request.query_params.get("error"),
        "flash_created": request.query_params.get("created"),
        "flash_renamed": request.query_params.get("renamed"),
        "flash_renamed_to": request.query_params.get("renamed_to"),
        "flash_lxc_action": request.query_params.get("lxc_action"),
        "flash_lxc_service": request.query_params.get("lxc_service"),
        "flash_lxc_task": request.query_params.get("lxc_task"),
        "flash_vm_action": request.query_params.get("vm_action"),
        "flash_vm_service": request.query_params.get("vm_service"),
        "flash_vm_task": request.query_params.get("vm_task"),
        "flash_docker_action": request.query_params.get("docker_action"),
        "flash_docker_service": request.query_params.get("docker_service"),
        "flash_docker_container": request.query_params.get("docker_container"),
        "flash_docker_task": request.query_params.get("docker_task"),
        **_csrf_context(),
    })


# ---------------------------------------------------------------------------
# New service — GET (show form) / POST (create)
# ---------------------------------------------------------------------------

@app.get("/services/new", response_class=HTMLResponse)
async def new_service_form(request: Request):
    return templates.TemplateResponse(request, "new_service.html", {
        "errors": [],
        "form_data": {},
        **_csrf_context(),
    })


@app.post("/services/new", response_class=HTMLResponse)
async def create_service(request: Request):
    form = await request.form()
    if not _verify_csrf(form):
        return _csrf_error_redirect()
    fd = dict(form)
    fd = _augment_form_with_domain_entries(fd)

    # --- validate with Pydantic ---
    form_errors: list[str] = []
    validated_service = None
    try:
        data = _parse_form(fd)
        validated_service = ServiceRecord(**data)
    except ValidationError as e:
        form_errors = [
            f"{'.'.join(str(loc) for loc in err['loc'])}: {err['msg']}"
            for err in e.errors()
        ]
    except (ValueError, TypeError) as e:
        form_errors = [str(e)]

    if form_errors:
        return templates.TemplateResponse(request, "new_service.html", {
            "errors": form_errors,
            "form_data": fd,
            **_csrf_context(),
        })

    # --- check slug conflict ---
    slug = validated_service.slug
    filepath = _service_path(slug)
    if filepath.exists():
        return templates.TemplateResponse(request, "new_service.html", {
            "errors": [f"A service with slug '{slug}' already exists."],
            "form_data": fd,
            **_csrf_context(),
        })

    # --- write YAML ---
    data = validated_service.model_dump(exclude_none=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    return _redirect_with_query(created=slug)


# ---------------------------------------------------------------------------
# Delete service
# ---------------------------------------------------------------------------

@app.post("/services/{slug}/delete")
async def delete_service(request: Request, slug: str):
    form = await request.form()
    if not _verify_csrf(form):
        return _csrf_error_redirect()
    try:
        filepath = _service_path(slug)
    except ValueError:
        return _redirect_with_query(error="Invalid service slug")
    if filepath.exists():
        filepath.unlink()
    return RedirectResponse(url="/", status_code=303)


# ---------------------------------------------------------------------------
# Toggle active/inactive
# ---------------------------------------------------------------------------

@app.post("/services/{slug}/toggle")
async def toggle_service(request: Request, slug: str):
    form = await request.form()
    if not _verify_csrf(form):
        return _csrf_error_redirect()
    try:
        filepath = _service_path(slug)
    except ValueError:
        return _redirect_with_query(error="Invalid service slug")
    if not filepath.exists():
        return RedirectResponse(url="/", status_code=303)
    with open(filepath) as f:
        data = yaml.safe_load(f)
    data["active"] = not data.get("active", True)
    with open(filepath, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
    return RedirectResponse(url="/", status_code=303)


@app.get("/services/{slug}/duplicate", response_class=HTMLResponse)
async def duplicate_service_form(request: Request, slug: str):
    try:
        filepath = _service_path(slug)
    except ValueError:
        return _redirect_with_query(error="Invalid service slug")
    if not filepath.exists():
        return RedirectResponse(url="/", status_code=303)
    with open(filepath) as f:
        raw = yaml.safe_load(f)
    try:
        svc = ServiceRecord(**raw)
    except Exception:
        return RedirectResponse(url="/", status_code=303)

    form_data = _service_to_form_data(svc)
    form_data["slug"] = _suggest_duplicate_slug(slug)
    form_data["name"] = f"{svc.name} Copy"

    return templates.TemplateResponse(request, "new_service.html", {
        "errors": [],
        "form_data": form_data,
        **_csrf_context(),
    })


@app.post("/services/{slug}/maintenance")
async def toggle_maintenance(request: Request, slug: str):
    try:
        filepath = _service_path(slug)
    except ValueError:
        return _redirect_with_query(path="/ops", error="Invalid service slug")
    if not filepath.exists():
        return RedirectResponse(url="/ops", status_code=303)
    form = await request.form()
    if not _verify_csrf(form):
        return _csrf_error_redirect("/ops")
    enabled = form.get("enabled", "0") == "1"
    reason = form.get("reason", "").strip()
    with open(filepath) as f:
        data = yaml.safe_load(f)
    monitoring = data.get("monitoring") or {}
    monitoring.setdefault("enabled", True)
    monitoring.setdefault("type", "http")
    monitoring["maintenance"] = enabled
    if enabled:
        monitoring["maintenance_reason"] = reason or "Planned maintenance"
    else:
        monitoring["maintenance_reason"] = reason
    data["monitoring"] = monitoring
    with open(filepath, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
    action_label = "enabled" if enabled else "disabled"
    return _redirect_with_query(path="/ops", maint_service=slug, maint_action=action_label)


@app.post("/services/{slug}/lxc/{action}")
async def lxc_action(request: Request, slug: str, action: str):
    form = await request.form()
    if not _verify_csrf(form):
        return _csrf_error_redirect()
    if action not in {"start", "stop", "restart"}:
        return _redirect_with_query(error="Unsupported LXC action")

    registry = _load_registry()
    service = next((svc for svc in registry.services if svc.slug == slug), None)
    if not service:
        return _redirect_with_query(error="Service not found")
    try:
        task_id = await run_lxc_action(service, registry.services, action)
    except ProxmoxError as exc:
        return _redirect_with_query(error=str(exc))

    return _redirect_with_query(
        lxc_action=action,
        lxc_service=service.name,
        lxc_task=task_id,
    )


@app.post("/services/{slug}/docker/{action}")
async def docker_action(request: Request, slug: str, action: str):
    form = await request.form()
    if not _verify_csrf(form):
        return _csrf_error_redirect()
    if action not in {"start", "stop", "restart"}:
        return _redirect_with_query(error="Unsupported Docker action")

    registry = _load_registry()
    service = next((svc for svc in registry.services if svc.slug == slug), None)
    if not service:
        return _redirect_with_query(error="Service not found")
    try:
        task_id = await run_docker_action(service, registry.services, action)
    except ProxmoxError as exc:
        return _redirect_with_query(error=str(exc))

    return _redirect_with_query(
        docker_action=action,
        docker_service=service.name,
        docker_container=service.runtime.container_name,
        docker_task=task_id,
    )


@app.post("/services/{slug}/vm/{action}")
async def vm_action(request: Request, slug: str, action: str):
    form = await request.form()
    if not _verify_csrf(form):
        return _csrf_error_redirect()
    if action not in {"start", "stop", "restart"}:
        return _redirect_with_query(error="Unsupported VM action")

    registry = _load_registry()
    service = next((svc for svc in registry.services if svc.slug == slug), None)
    if not service:
        return _redirect_with_query(error="Service not found")
    try:
        task_id = await run_vm_action(service, registry.services, action)
    except ProxmoxError as exc:
        return _redirect_with_query(error=str(exc))

    return _redirect_with_query(
        vm_action=action,
        vm_service=service.name,
        vm_task=task_id,
    )


# ---------------------------------------------------------------------------
# Edit service — GET (show pre-filled form) / POST (save)
# ---------------------------------------------------------------------------

@app.get("/services/{slug}/edit", response_class=HTMLResponse)
async def edit_service_form(request: Request, slug: str):
    try:
        filepath = _service_path(slug)
    except ValueError:
        return _redirect_with_query(error="Invalid service slug")
    if not filepath.exists():
        return RedirectResponse(url="/", status_code=303)
    with open(filepath) as f:
        raw = yaml.safe_load(f)
    try:
        svc = ServiceRecord(**raw)
    except Exception:
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request, "new_service.html", {
        "errors": [],
        "form_data": _service_to_form_data(svc),
        "edit_mode": True,
        "slug": slug,
        **_csrf_context(),
    })


@app.post("/services/{slug}/edit", response_class=HTMLResponse)
async def edit_service(request: Request, slug: str):
    form = await request.form()
    if not _verify_csrf(form):
        return _csrf_error_redirect()
    fd = dict(form)
    fd["slug"] = slug  # slug is fixed from the URL
    fd = _augment_form_with_domain_entries(fd)

    form_errors: list[str] = []
    validated_service = None
    try:
        data = _parse_form(fd)
        validated_service = ServiceRecord(**data)
    except ValidationError as e:
        form_errors = [
            f"{'.' .join(str(loc) for loc in err['loc'])}: {err['msg']}"
            for err in e.errors()
        ]
    except (ValueError, TypeError) as e:
        form_errors = [str(e)]

    if form_errors:
        return templates.TemplateResponse(request, "new_service.html", {
            "errors": form_errors,
            "form_data": fd,
            "edit_mode": True,
            "slug": slug,
            **_csrf_context(),
        })

    filepath = _service_path(validated_service.slug)
    data = validated_service.model_dump(exclude_none=True)
    with open(filepath, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    return RedirectResponse(url="/", status_code=303)


# ---------------------------------------------------------------------------
# Import from Nginx Proxy Manager
# ---------------------------------------------------------------------------


@app.post("/render/{target}")
async def render_config(request: Request, target: str):
    form = await request.form()
    if not _verify_csrf(form):
        return _csrf_error_redirect("/config")
    if target not in {"mafl", "ansible", "monitoring"}:
        return _redirect_with_query(path="/config", error="Unsupported render target")

    try:
        registry = _load_valid_registry()
        if target == "mafl":
            render_mafl(registry.services)
        elif target == "ansible":
            render_ansible(registry.services)
        elif target == "monitoring":
            render_monitoring(registry.services)
    except Exception as exc:
        return _redirect_with_query(path="/config", error=f"Render {target} failed: {str(exc)[:120]}")

    return _redirect_with_query(path="/config", config_action="rendered", config_target=target)


@app.post("/sync/mafl")
async def sync_mafl_config(request: Request):
    form = await request.form()
    if not _verify_csrf(form):
        return _csrf_error_redirect("/config")

    try:
        registry = _load_valid_registry()
        await sync_mafl(registry.services)
    except Exception as exc:
        app_logger.exception("Mafl deploy failed")
        _health_log(event="deploy_error", target="mafl", error=str(exc))
        return _redirect_with_query(path="/config", error=f"Mafl deploy failed: {str(exc)[:120]}")

    return _redirect_with_query(path="/config", config_action="deployed", config_target="mafl")


@app.post("/import/npm")
async def import_from_npm(request: Request):
    form = await request.form()
    if not _verify_csrf(form):
        return _csrf_error_redirect("/config")
    npm_url = os.environ.get("NPM_URL", "").strip().rstrip("/")
    email   = os.environ.get("NPM_EMAIL", "").strip()
    password = os.environ.get("NPM_PASSWORD", "").strip()

    if not npm_url or not email or not password:
        err = "NPM credentials not configured (set NPM_URL, NPM_EMAIL, NPM_PASSWORD)"
        return _redirect_with_query(path="/config", error=err)

    async with httpx.AsyncClient(follow_redirects=True) as npm_client:
        try:
            resp = await npm_client.post(
                f"{npm_url}/api/tokens",
                json={"identity": email, "secret": password},
                timeout=10,
            )
            resp.raise_for_status()
            token = resp.json()["token"]
        except Exception as e:
            return _redirect_with_query(path="/config", error=f"NPM auth failed: {str(e)[:80]}")

        try:
            resp = await npm_client.get(
                f"{npm_url}/api/nginx/proxy-hosts",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            resp.raise_for_status()
            hosts = resp.json()
        except Exception as e:
            return _redirect_with_query(path="/config", error=f"Failed to fetch NPM hosts: {str(e)[:80]}")

    imported = skipped = 0
    for host in hosts:
        domains = host.get("domain_names", [])
        if not domains:
            continue
        domain = domains[0]
        slug = domain.split(".")[0]
        if not SLUG_RE.fullmatch(slug):
            skipped += 1
            continue
        filepath = _service_path(slug)
        if filepath.exists():
            skipped += 1
            continue
        data = {
            "slug": slug,
            "name": slug.replace("-", " ").title(),
            "group": "Imported",
            "description": "Imported from Nginx Proxy Manager",
            "tags": ["imported"],
            "network": {
                "vlan": "imported",
                "host": host.get("forward_host", slug),
                "ip": host.get("forward_host", ""),
                "dns": domains,
            },
            "backend": {
                "scheme": host.get("forward_scheme", "http"),
                "port": int(host.get("forward_port", 80)),
            },
            "exposure": {
                "homepage": False,
                "reverse_proxy": True,
                "public": False,
                "force_ssl": bool(host.get("ssl_forced", False)),
            },
        }
        try:
            data = ServiceRecord(**data).model_dump(exclude_none=True)
        except Exception:
            skipped += 1
            continue
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(filepath, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        imported += 1

    return _redirect_with_query(path="/config", imported=imported, skipped=skipped)


# ---------------------------------------------------------------------------
# Rename group — bulk update group field across services
# ---------------------------------------------------------------------------

@app.post("/services/rename-group")
async def rename_group(request: Request):
    form = await request.form()
    if not _verify_csrf(form):
        return _csrf_error_redirect()
    old_group = form.get("old_group", "").strip()
    new_group = form.get("new_group", "").strip()
    if not old_group or not new_group:
        return _redirect_with_query(error="Both group names are required")
    registry = _load_registry()
    updated = 0
    for svc in registry.services:
        if svc.group == old_group:
            filepath = _service_path(svc.slug)
            with open(filepath) as f:
                data = yaml.safe_load(f)
            data["group"] = new_group
            with open(filepath, "w") as f:
                yaml.dump(data, f, default_flow_style=False, sort_keys=False)
            updated += 1
    return _redirect_with_query(renamed=updated, renamed_to=new_group)


# ---------------------------------------------------------------------------
# Sync from Nginx Proxy Manager — update existing + create new
# ---------------------------------------------------------------------------

@app.post("/sync/npm")
async def sync_from_npm(request: Request):
    form = await request.form()
    if not _verify_csrf(form):
        return _csrf_error_redirect("/config")
    npm_url = os.environ.get("NPM_URL", "").strip().rstrip("/")
    email    = os.environ.get("NPM_EMAIL", "").strip()
    password = os.environ.get("NPM_PASSWORD", "").strip()

    if not npm_url or not email or not password:
        err = "NPM credentials not configured (set NPM_URL, NPM_EMAIL, NPM_PASSWORD)"
        return _redirect_with_query(path="/config", error=err)

    async with httpx.AsyncClient(follow_redirects=True) as npm_client:
        try:
            resp = await npm_client.post(
                f"{npm_url}/api/tokens",
                json={"identity": email, "secret": password},
                timeout=10,
            )
            resp.raise_for_status()
            token = resp.json()["token"]
        except Exception as e:
            return _redirect_with_query(path="/config", error=f"NPM auth failed: {str(e)[:80]}")

        try:
            resp = await npm_client.get(
                f"{npm_url}/api/nginx/proxy-hosts",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            resp.raise_for_status()
            hosts = resp.json()
        except Exception as e:
            return _redirect_with_query(path="/config", error=f"Failed to fetch NPM hosts: {str(e)[:80]}")

    created = updated = skipped = 0
    for host in hosts:
        domains = host.get("domain_names", [])
        if not domains:
            continue
        domain = domains[0]
        slug = domain.split(".")[0]
        if not SLUG_RE.fullmatch(slug):
            skipped += 1
            continue
        filepath = _service_path(slug)

        npm_network = {
            "host": host.get("forward_host", slug),
            "ip": host.get("forward_host", ""),
            "dns": domains,
        }
        npm_backend = {
            "scheme": host.get("forward_scheme", "http"),
            "port": int(host.get("forward_port", 80)),
        }
        npm_exposure = {
            "reverse_proxy": True,
            "force_ssl": bool(host.get("ssl_forced", False)),
        }

        if filepath.exists():
            with open(filepath) as f:
                data = yaml.safe_load(f)
            data.setdefault("network", {}).update(npm_network)
            data.setdefault("backend", {}).update(npm_backend)
            data.setdefault("exposure", {}).update(npm_exposure)
            try:
                data = ServiceRecord(**data).model_dump(exclude_none=True)
                with open(filepath, "w") as f:
                    yaml.dump(data, f, default_flow_style=False, sort_keys=False)
                updated += 1
            except Exception:
                skipped += 1
        else:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            new_data = {
                "slug": slug,
                "name": slug.replace("-", " ").title(),
                "group": "Imported",
                "description": "Imported from Nginx Proxy Manager",
                "tags": ["imported"],
                "network": {"vlan": "imported", **npm_network},
                "backend": {**npm_backend},
                "exposure": {"homepage": False, "public": False, **npm_exposure},
            }
            try:
                new_data = ServiceRecord(**new_data).model_dump(exclude_none=True)
            except Exception:
                skipped += 1
                continue
            with open(filepath, "w") as f:
                yaml.dump(new_data, f, default_flow_style=False, sort_keys=False)
            created += 1

    return _redirect_with_query(path="/config", synced=updated, created=created, skipped=skipped)


# ---------------------------------------------------------------------------
# Operations, Config, Logs pages
# ---------------------------------------------------------------------------

@app.get("/ops", response_class=HTMLResponse)
async def ops_page(request: Request):
    registry = _load_registry()
    active_services = sorted(
        [s for s in registry.services if s.active],
        key=lambda s: (s.group.lower(), s.name.lower()),
    )
    return templates.TemplateResponse(request, "ops.html", {
        "active_services": active_services,
        "flash_maint_service": request.query_params.get("maint_service"),
        "flash_maint_action": request.query_params.get("maint_action"),
        "flash_error": request.query_params.get("error"),
        **_csrf_context(),
    })


@app.get("/config", response_class=HTMLResponse)
async def config_page(request: Request):
    npm_configured = bool(
        os.environ.get("NPM_URL", "").strip()
        and os.environ.get("NPM_EMAIL", "").strip()
        and os.environ.get("NPM_PASSWORD", "").strip()
    )
    return templates.TemplateResponse(request, "config.html", {
        "npm_configured": npm_configured,
        "flash_config_action": request.query_params.get("config_action"),
        "flash_config_target": request.query_params.get("config_target"),
        "flash_error": request.query_params.get("error"),
        "flash_imported": request.query_params.get("imported"),
        "flash_synced": request.query_params.get("synced"),
        "flash_created": request.query_params.get("created"),
        "flash_skipped": request.query_params.get("skipped"),
        **_csrf_context(),
    })


@app.get("/backup/download")
async def backup_download():
    buf = io.BytesIO()
    data_dir = Path("data")
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(data_dir.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(data_dir.parent))
    buf.seek(0)
    from datetime import datetime
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"arda-registry-backup-{stamp}.zip"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/backup/restore")
async def backup_restore(request: Request, file: UploadFile = File(...)):
    form = await request.form()
    if not _verify_csrf(form):
        return _csrf_error_redirect("/config")

    if not file.filename or not file.filename.endswith(".zip"):
        return _redirect_with_query(path="/config", error="Restore failed: file must be a .zip")

    content = await file.read()
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            for name in zf.namelist():
                resolved = Path(name).resolve()
                if not str(resolved).startswith(str(Path("data").resolve())):
                    raise ValueError(f"Unsafe path in archive: {name}")
                if not name.startswith("data/"):
                    raise ValueError(f"Archive must contain only data/ paths, got: {name}")
            zf.extractall(".")
    except (zipfile.BadZipFile, ValueError) as exc:
        return _redirect_with_query(path="/config", error=f"Restore failed: {exc}")

    app_logger.info("Registry restored from backup: %s", file.filename)
    _health_log(event="backup_restore", filename=file.filename)
    return _redirect_with_query(path="/config", config_action="restored", config_target="backup")


@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    entries = _read_log_entries(500)
    event_types = sorted({e.get("event", "unknown") for e in entries})
    return templates.TemplateResponse(request, "logs.html", {
        "entries": entries,
        "event_types": event_types,
    })
