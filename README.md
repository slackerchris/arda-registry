# Arda Registry

Arda Registry is a homelab control plane designed to centralize service intent. Instead of manually configuring proxies, dashboards, and monitoring tools individually, you define your services once in a simple YAML format. The registry acts as the single source of truth, validating schemas and generating configurations for downstream tools.

## Features

- **Single Source of Truth**: Manage all homelab services via declarative YAML definitions.
- **Strict Validation**: Pydantic-powered schema enforcement — the validator catches duplicate IP:port combinations, missing required fields, and invalid IPs at load time.
- **Multi-page Web UI**: Services, Operations, Config, and Logs pages.
- **Proxmox Integration**: Start, stop, and restart LXC containers and QEMU VMs directly from the UI.
- **Power Status**: Live power state (running / stopped / paused) displayed on service cards.
- **Health Monitoring**: HTTP, TCP, and ping health checks with debounced state transitions, maintenance mode, and structured JSON log output.
- **Generators**:
  - **Mafl Dashboard**: Merges registry services into `data/mafl.yml` and deploys to a shared NAS-backed config path.
  - **Nginx Proxy Manager (NPM)**: Imports and syncs reverse proxy hosts over the NPM API.
  - **Ansible**: Generates dynamic inventory files grouped by VLAN, keyed by service slug.
- **Backup & Restore**: Download a ZIP of all service configs; restore from a previous backup via the Config page.

---

## Getting Started

### Prerequisites

- Python 3.10+
- A virtual environment (recommended)

### Installation

```bash
cd arda-registry
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Run the Web UI

```bash
PYTHONPATH=. python app/main.py serve
```

Binds to `127.0.0.1:8888` by default. To expose on the network, set auth credentials first:

```bash
export ARDA_UI_USERNAME=arda
export ARDA_UI_PASSWORD=change-me
PYTHONPATH=. python app/main.py serve --host 0.0.0.0
```

### Run With Docker

```bash
cp .env.example .env
docker compose up -d --build
```

The compose file mounts `./data`, `./output`, and `/mnt/nas-downloads/mafl` so service definitions, logs, state, and Mafl landing-zone renders stay outside the container image.

### Validate the Registry

```bash
PYTHONPATH=. python app/main.py validate
```

Checks all YAML files in `data/services/` against the Pydantic schema and reports cross-service conflicts (e.g. duplicate IP:port).

### Render Configurations

```bash
PYTHONPATH=. python app/main.py render mafl
PYTHONPATH=. python app/main.py render ansible
```

### Sync API Services

```bash
PYTHONPATH=. python app/main.py sync npm
PYTHONPATH=. python app/main.py sync mafl
```

NPM sync requires `NPM_URL`, `NPM_EMAIL`, and `NPM_PASSWORD` environment variables. Without them the sync runs in dry-run mode.

---

## Service YAML Schema

Example (`data/services/jellyfin.yml`):

```yaml
slug: jellyfin
name: Jellyfin
app: jellyfin          # optional — canonical app name used for Mafl icon (mdi:<app>)
group: Media
description: Family media server
tags:
  - media

network:
  vlan: media
  ip: 10.0.20.55
  dns: jellyfin.throne.middl.earth

backend:
  scheme: http
  port: 8096
  health_path: /health

exposure:
  homepage: true        # include in Mafl dashboard
  reverse_proxy: true   # service is behind NPM; use domain URL in links
  public: false
  force_ssl: true

monitoring:
  enabled: true
  type: http

runtime:
  type: lxc             # lxc | vm | docker | bare
  host: helmsdeep       # Proxmox node name
  container_id: 116     # LXC vmid or QEMU vmid
  # container_name: jellyfin  # Docker containers — name inside the LXC

api:
  base_url: http://10.0.20.55:8096
  key: $JELLYFIN_API_KEY
  key_header: X-MediaBrowser-Token
  status_path: /health
```

### Key fields

| Field | Description |
|---|---|
| `slug` | Unique identifier, used as the filename. Letters, numbers, `-`, `_`. |
| `app` | Canonical app name for icon lookup (e.g. `radarr`, `sonarr`). Defaults to `slug`. |
| `network.ip` | Required. IP address of the service. Used for routing and duplicate detection. |
| `exposure.homepage` | Show this service in the Mafl homepage dashboard. |
| `exposure.reverse_proxy` | Service is behind a reverse proxy; generated links use the domain name. |
| `runtime.type` | `lxc`, `vm`, `docker`, or `bare`. Enables Proxmox power controls in the UI. |
| `runtime.host` | Proxmox node name (e.g. `helmsdeep`). |
| `runtime.container_id` | LXC or QEMU vmid. |
| `runtime.container_name` | Docker container name inside the LXC (Docker type). |
| `api.key` | API key — use `$ENV_VAR` syntax, never raw values. |
| `api.key_header` | Header name for the API key (e.g. `X-Api-Key`, `Authorization`). |

---

## Proxmox Integration

### Power Management

Services with `runtime.type: lxc`, `vm`, or `docker` get Start / Restart / Stop buttons in the UI. The UI polls the Proxmox API for live power state (running / stopped / paused) and shows it on each card.

Configure nodes in `data/integrations.yml`:

```yaml
proxmox:
  key: $PVE_API_TOKEN
  verify_ssl: false
  nodes:
    helmsdeep:
      base_url: https://10.0.99.4:8006
    mountdoom:
      base_url: https://10.0.99.2:8006
```

Set the token before starting the server:

```bash
export PVE_API_TOKEN=PVEAPIToken=user@pam!token-name=secret
```

The token requires `VM.PowerMgmt` on `/vms` and `VM.Audit` to read power state. To grant it:

```bash
pveum acl set /vms --user ansible@pve --roles PVEVMAdmin
```

### Mafl Deploy Modes

Arda supports two Mafl deploy modes. Use `landing` when Arda and Mafl are on different hosts sharing a NAS path. Use `direct` when Arda runs on the same box as Mafl and can mount the live Mafl config directory.

#### Landing Mode

Landing mode writes the rendered Mafl config to a NAS landing zone and writes a small deploy request next to it. A local mover inside the Mafl LXC promotes the file into the live `/docker/mafl` config directory and restarts Mafl. This avoids SSH or Proxmox exec from Arda:

```yaml
mafl:
  source_path: data/mafl.yml
  output_path: /mnt/nas-downloads/mafl/config.yml   # NAS mount on this host
  deploy:
    mode: landing
    nas_path: /mnt/downloads/mafl/config.yml   # same landing file, as seen from inside the LXC
    path: /docker/mafl/config.yml              # live config path inside the LXC
    restart_command: docker restart mafl
```

Deploy flow:
1. Render writes `config.yml` to the NAS mount on this host
2. Arda writes `config.deploy.yml` beside it with the LXC-side source/destination paths
3. A systemd path unit inside the LXC runs `mafl-promote.sh`
4. The mover backs up `/docker/mafl/config.yml`, copies in the landing config, and restarts Mafl

Install the mover inside the Mafl LXC:

```bash
install -m 0755 deploy/mafl-promote.sh /usr/local/sbin/mafl-promote.sh
install -m 0644 deploy/mafl-promote.service /etc/systemd/system/mafl-promote.service
install -m 0644 deploy/mafl-promote.path /etc/systemd/system/mafl-promote.path
systemctl daemon-reload
systemctl enable --now mafl-promote.path
```

If the live directory ever changes, edit `MAFL_DEST_FILE` in `mafl-promote.service` and `mafl.deploy.path` in `data/integrations.yml` to match.

#### Direct Mode

Direct mode writes straight to Mafl's live config path. This skips the NAS sidecar request and the LXC mover. Mount the live config directory into the Arda container:

```yaml
services:
  arda-registry:
    volumes:
      - ./data:/app/data
      - ./output:/app/output
      - /docker/mafl:/docker/mafl
```

Then configure:

```yaml
mafl:
  source_path: data/mafl.yml
  output_path: /docker/mafl/config.yml
  deploy:
    mode: direct
    path: /docker/mafl/config.yml
```

Direct mode writes the file only. Restart Mafl separately unless you intentionally give Arda access to Docker control.

---

## Ops Health Checks

The `/api/status` endpoint runs health checks for all active services with monitoring enabled. The app caches state to avoid excessive checks.

### Status Colors

| Color | Meaning |
|---|---|
| `green` | All monitored services are up |
| `yellow` | No services down, but one or more in maintenance |
| `red` | One or more services are down |

### Debounce Behavior

- A service requires **N consecutive failures** before flipping to `down`
- A service requires **N consecutive successes** before recovering to `up`
- Each check retries before counting as a failure

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `OPS_CHECK_MIN_SECONDS` | `300` | Seconds between full refreshes |
| `OPS_FAIL_THRESHOLD` | `2` | Consecutive failures to mark down |
| `OPS_RECOVERY_THRESHOLD` | `2` | Consecutive successes to recover |
| `OPS_CHECK_RETRIES` | `2` | Retry attempts per check |
| `OPS_CHECK_RETRY_DELAY_SECONDS` | `0.35` | Delay between retries |

### State Files

| File | Contents |
|---|---|
| `output/state/ops_state.json` | Per-service status, timestamps, summary history |
| `output/logs/ops_health.log` | Structured JSON log — health checks, summaries, deploy events |

---

## Backup & Restore

The Config page provides a **Download Backup** button that creates a timestamped ZIP of the entire `data/` directory (service YAMLs, `integrations.yml`, `mafl.yml`).

To restore, upload a backup ZIP via the **Restore** button on the same page. The restore overwrites all files in `data/` with the archive contents. The restore rejects path traversal — the archive must contain `data/` paths.

---

## UI Pages

| Page | Path | Description |
|---|---|---|
| Services | `/` | Service cards with status dots, power state, and power controls |
| Operations | `/ops` | Health table, UniFi summary, maintenance toggles |
| Config | `/config` | Generators, NPM sync, group rename, backup & restore |
| Logs | `/logs` | Structured event log with filter and search |

---

## Architecture

```
data/services/*.yml       ← declarative service definitions
data/integrations.yml     ← Proxmox, Mafl, NPM credentials
data/mafl.yml             ← hand-crafted Mafl base config
       │
       ▼
[ Arda Registry ]
       │
       ├─► /mnt/nas-.../mafl/config.yml   → shared NAS mount → Mafl
       ├─► output/ansible/inventory.yml
       ├─► NPM API (sync)
       ├─► output/state/ops_state.json
       └─► output/logs/ops_health.log
```
