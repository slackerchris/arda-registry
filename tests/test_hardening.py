import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import web
from app.integrations import IntegrationsConfig, load_integrations
from app.generators.mafl import render_mafl, sync_mafl
from app.models import ServiceRecord
from app.proxmox import run_lxc_action


def valid_service(**overrides):
    data = {
        "slug": "demo",
        "name": "Demo",
        "group": "Test",
        "description": "Demo service",
        "network": {
            "vlan": "test",
            "host": "demo",
            "ip": "10.0.0.10",
            "dns": ["demo.example.internal"],
        },
        "backend": {"scheme": "http", "port": 8080},
        "exposure": {"dashboard": False, "reverse_proxy": False, "public": False},
    }
    data.update(overrides)
    return data


class HardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_data_dir = web.DATA_DIR
        web.DATA_DIR = Path(self.tmp.name) / "services"
        self.client = TestClient(web.app, raise_server_exceptions=False)

    def tearDown(self):
        web.DATA_DIR = self.old_data_dir
        self.tmp.cleanup()

    def post_data(self, data):
        return {"csrf_token": web._csrf_token(), **data}

    def test_model_rejects_unsafe_slug_and_invalid_backend(self):
        with self.assertRaises(ValidationError):
            ServiceRecord(**valid_service(slug="../escape"))
        with self.assertRaises(ValidationError):
            ServiceRecord(**valid_service(network={"vlan": "x", "host": "x", "ip": "not-an-ip"}))
        with self.assertRaises(ValidationError):
            ServiceRecord(**valid_service(backend={"scheme": "gopher", "port": 70000}))

    def test_model_requires_api_keys_to_use_environment_references(self):
        with self.assertRaises(ValidationError):
            ServiceRecord(**valid_service(api={"key": "raw-secret"}))
        svc = ServiceRecord(**valid_service(api={"key": "$DEMO_API_KEY"}))
        self.assertEqual(svc.api.key, "$DEMO_API_KEY")

    def test_api_headers_apply_key_prefix(self):
        service = ServiceRecord(
            **valid_service(
                api={
                    "base_url": "https://truenas.example.internal",
                    "key": "$TEST_TRUENAS_API_KEY",
                    "key_header": "Authorization",
                    "key_prefix": "Bearer",
                    "status_path": "/api/v2.0/alert/list",
                }
            )
        )

        with patch.dict("os.environ", {"TEST_TRUENAS_API_KEY": "secret"}, clear=False):
            headers = web._api_headers_for_service(service)

        self.assertEqual(headers["Authorization"], "Bearer secret")

    def test_integrations_config_loads_shared_proxmox_object(self):
        config_path = Path(self.tmp.name) / "integrations.yml"
        config_path.write_text(
            yaml.dump(
                {
                    "proxmox": {
                        "key": "$PVE_API_TOKEN",
                        "verify_ssl": False,
                        "nodes": {"helmsdeep": {"base_url": "https://10.0.99.4:8006"}},
                    }
                }
            ),
            encoding="utf-8",
        )
        config = load_integrations(config_path)
        self.assertEqual(config.proxmox.key, "$PVE_API_TOKEN")
        self.assertEqual(config.proxmox.nodes["helmsdeep"].base_url, "https://10.0.99.4:8006")

    def test_mafl_paths_can_be_overridden_by_environment(self):
        config_path = Path(self.tmp.name) / "integrations.yml"
        config_path.write_text(
            yaml.dump(
                {
                    "mafl": {
                        "output_path": "/old/config.yml",
                        "deploy": {
                            "mode": "landing",
                            "nas_path": "/old/nas/config.yml",
                            "path": "/old/live/config.yml",
                        },
                    }
                }
            ),
            encoding="utf-8",
        )

        with patch.dict(
            "os.environ",
            {
                "MAFL_DEPLOY_MODE": "direct",
                "MAFL_OUTPUT_PATH": "/docker/mafl/config.yml",
                "MAFL_NAS_PATH": "/mnt/downloads/mafl/config.yml",
                "MAFL_LIVE_PATH": "/docker/mafl/config.yml",
                "MAFL_RESTART_COMMAND": "docker restart mafl",
            },
            clear=False,
        ):
            config = load_integrations(config_path)

        self.assertEqual(config.mafl.output_path, "/docker/mafl/config.yml")
        self.assertEqual(config.mafl.deploy.mode, "direct")
        self.assertEqual(config.mafl.deploy.nas_path, "/mnt/downloads/mafl/config.yml")
        self.assertEqual(config.mafl.deploy.path, "/docker/mafl/config.yml")
        self.assertEqual(config.mafl.deploy.restart_command, "docker restart mafl")

    def test_create_form_validation_errors_do_not_500(self):
        resp = self.client.post(
            "/services/new",
            data=self.post_data(
                {
                    "slug": "bad",
                    "name": "Bad",
                    "group": "Test",
                    "description": "Bad",
                    "network_vlan": "test",
                    "network_host": "bad",
                    "network_ip": "not-an-ip",
                    "backend_scheme": "http",
                    "backend_port": "not-a-port",
                }
            ),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("network.ip", resp.text)
        self.assertIn("backend.port", resp.text)

    def test_create_form_rejects_path_traversal_slug(self):
        resp = self.client.post(
            "/services/new",
            data=self.post_data(
                {
                    "slug": "../escape",
                    "name": "Escape",
                    "group": "Test",
                    "description": "Escape",
                    "network_vlan": "test",
                    "network_host": "escape",
                    "network_ip": "10.0.0.20",
                    "backend_scheme": "http",
                    "backend_port": "80",
                }
            ),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("slug must use only", resp.text)
        self.assertFalse((Path(self.tmp.name) / "escape.yml").exists())

    def test_post_requires_csrf_token(self):
        resp = self.client.post("/services/new", data={}, follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertIn("error=", resp.headers["location"])

    def test_configured_auth_is_required(self):
        with patch.dict("os.environ", {"ARDA_UI_PASSWORD": "secret"}, clear=False):
            unauthenticated = self.client.get("/")
            authenticated = self.client.get("/", auth=("arda", "secret"))
        self.assertEqual(unauthenticated.status_code, 401)
        self.assertEqual(authenticated.status_code, 200)

    def test_proxmox_lxc_action_uses_runtime_host_and_container_id(self):
        service = ServiceRecord(
            **valid_service(
                slug="tv",
                runtime={"type": "lxc", "host": "helmsdeep", "container_id": 400},
            )
        )
        node = ServiceRecord(
            **valid_service(
                slug="helmsdeep",
                name="helmsdeep",
                network={"vlan": "99", "host": "helmsdeep", "ip": "10.0.99.4"},
                backend={"scheme": "https", "port": 8006},
            )
        )

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"data": "UPID:helmsdeep:test"}

        integrations = IntegrationsConfig(
            proxmox={
                "key": "$TEST_PVE_TOKEN",
                "nodes": {"helmsdeep": {"base_url": "https://10.0.99.4:8006"}},
            }
        )
        import asyncio

        mock_post = AsyncMock(return_value=FakeResponse())
        mock_client = MagicMock()
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch.dict("os.environ", {"TEST_PVE_TOKEN": "ansible@pve!arda=secret"}, clear=False):
            with patch("app.proxmox.load_integrations", return_value=integrations):
                with patch("app.proxmox.httpx.AsyncClient", return_value=mock_client) as MockClient:
                    task_id = asyncio.run(run_lxc_action(service, [service, node], "restart"))

        self.assertEqual(task_id, "UPID:helmsdeep:test")
        MockClient.assert_called_once_with(verify=False)
        mock_post.assert_called_once_with(
            "https://10.0.99.4:8006/api2/json/nodes/helmsdeep/lxc/400/status/reboot",
            headers={"Authorization": "PVEAPIToken=ansible@pve!arda=secret"},
            timeout=15,
        )

    def test_proxmox_health_headers_use_shared_integration_token(self):
        service = ServiceRecord(
            **valid_service(
                slug="brandwine",
                name="brandywine",
                tags=["proxmox"],
                network={"vlan": "99", "host": "brandywine", "ip": "10.0.99.3"},
                backend={"scheme": "https", "port": 8006},
                api={
                    "base_url": "https://10.0.99.3:8006",
                    "status_path": "/api2/json/version",
                    "verify_ssl": True,
                },
            )
        )
        integrations = IntegrationsConfig(
            proxmox={
                "key": "$TEST_PVE_TOKEN",
                "verify_ssl": False,
                "nodes": {"cluster": {"base_url": "https://10.0.99.3:8006", "verify_ssl": False}},
            }
        )
        with patch.dict("os.environ", {"TEST_PVE_TOKEN": "ansible@pve!arda=secret"}, clear=False):
            with patch("app.web.load_integrations", return_value=integrations):
                headers = web._api_headers_for_service(service)
                verify_ssl = web._verify_ssl_for_service(service)

        self.assertEqual(headers["Authorization"], "PVEAPIToken=ansible@pve!arda=secret")
        self.assertFalse(verify_ssl)

    def test_unifi_api_key_alias_is_used_for_ops_summary(self):
        web.DATA_DIR.mkdir(parents=True, exist_ok=True)
        service = valid_service(
            slug="unifi",
            name="UniFi Controller",
            network={"vlan": "0", "ip": "10.0.0.1", "dns": ["unifi.local"]},
            backend={"scheme": "https", "port": 443},
            api={
                "base_url": "https://10.0.0.1",
                "key": "$UNIFI_API_TOKEN",
                "key_header": "X-API-KEY",
                "status_path": "/proxy/network/api/s/default/stat/device",
                "verify_ssl": False,
            },
        )
        (web.DATA_DIR / "unifi.yml").write_text(yaml.dump(service), encoding="utf-8")

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"data": []}

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.headers = {}
                self.urls = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, url, **kwargs):
                self.urls.append(url)
                return FakeResponse()

        fake_client = FakeClient()
        with patch.dict("os.environ", {"UNIFI_API_KEY": "alias-secret"}, clear=False):
            with patch("app.web.httpx.AsyncClient", return_value=fake_client):
                import asyncio

                result = asyncio.run(web._fetch_unifi_summary())

        self.assertTrue(result["up"])
        self.assertEqual(fake_client.headers["X-API-KEY"], "alias-secret")
        self.assertTrue(all(url.startswith("https://10.0.0.1/") for url in fake_client.urls))

    def test_auth_failure_status_codes_are_down(self):
        class FakeResponse:
            status_code = 401

        class FakeClient:
            async def get(self, *args, **kwargs):
                return FakeResponse()

        service = ServiceRecord(
            **valid_service(
                slug="demo",
                api={"base_url": "https://demo.example", "status_path": "/status"},
            )
        )
        import asyncio

        result = asyncio.run(web._check_health(service, FakeClient()))
        self.assertFalse(result["up"])
        self.assertEqual(result["status_code"], 401)
        self.assertEqual(result["error"], "HTTP 401")

    def test_lxc_route_requires_csrf_and_dispatches_action(self):
        web.DATA_DIR.mkdir(parents=True, exist_ok=True)
        node = valid_service(
            slug="helmsdeep",
            name="helmsdeep",
            network={"vlan": "99", "host": "helmsdeep", "ip": "10.0.99.4"},
            backend={"scheme": "https", "port": 8006},
        )
        service = valid_service(
            slug="tv",
            runtime={"type": "lxc", "host": "helmsdeep", "container_id": 400},
        )
        (web.DATA_DIR / "helmsdeep.yml").write_text(yaml.dump(node), encoding="utf-8")
        (web.DATA_DIR / "tv.yml").write_text(yaml.dump(service), encoding="utf-8")

        missing_csrf = self.client.post("/services/tv/lxc/restart", data={}, follow_redirects=False)
        self.assertEqual(missing_csrf.status_code, 303)

        with patch("app.web.run_lxc_action", new=AsyncMock(return_value="UPID:task")) as action:
            resp = self.client.post(
                "/services/tv/lxc/restart",
                data={"csrf_token": web._csrf_token()},
                follow_redirects=False,
            )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("lxc_action=restart", resp.headers["location"])
        action.assert_called_once()

    def test_mafl_renderer_defaults_to_grouped_config_shape(self):
        source = Path(self.tmp.name) / "mafl.yml"
        output = Path(self.tmp.name) / "rendered.yml"
        source.write_text(
            yaml.dump(
                {
                    "title": "Middle Earth Labs",
                    "services": {
                        "AI & Development": [{"title": "Open WebUI", "link": "https://ai/"}],
                        "Infrastructure": [{"title": "Proxmox VE", "link": "https://pve/"}],
                    },
                }
            ),
            encoding="utf-8",
        )
        integrations = IntegrationsConfig(mafl={"source_path": str(source), "output_path": str(output)})
        with patch("app.generators.mafl.load_integrations", return_value=integrations):
            render_mafl([])

        rendered = yaml.safe_load(output.read_text(encoding="utf-8"))
        self.assertEqual(rendered["title"], "Middle Earth Labs")
        self.assertIsInstance(rendered["services"], dict)
        self.assertIn("AI & Development", rendered["services"])
        self.assertEqual(rendered["services"]["Infrastructure"][0]["title"], "Proxmox VE")

    def test_mafl_renderer_uses_safe_default_icon_for_unknown_apps(self):
        source = Path(self.tmp.name) / "mafl.yml"
        output = Path(self.tmp.name) / "rendered.yml"
        source.write_text(yaml.dump({"title": "Middle Earth Labs", "services": {}}), encoding="utf-8")
        service = ServiceRecord(
            **valid_service(
                slug="bookscout",
                name="Bookscout",
                group="Media & Automation",
                description="Imported from Nginx Proxy Manager",
                exposure={"homepage": True, "reverse_proxy": True, "public": False},
            )
        )
        integrations = IntegrationsConfig(mafl={"source_path": str(source), "output_path": str(output)})
        with patch("app.generators.mafl.load_integrations", return_value=integrations):
            render_mafl([service])

        rendered = yaml.safe_load(output.read_text(encoding="utf-8"))
        item = rendered["services"]["Media & Automation"][0]
        self.assertEqual(item["icon"]["name"], "mdi:web")

    def test_mafl_renderer_adds_known_icon_brand_colors(self):
        source = Path(self.tmp.name) / "mafl.yml"
        output = Path(self.tmp.name) / "rendered.yml"
        source.write_text(yaml.dump({"title": "Middle Earth Labs", "services": {}}), encoding="utf-8")
        service = ServiceRecord(
            **valid_service(
                slug="proxmox",
                name="Proxmox VE",
                app="simple-icons:proxmox",
                group="Infrastructure",
                exposure={"homepage": True, "reverse_proxy": True, "public": False},
            )
        )
        integrations = IntegrationsConfig(mafl={"source_path": str(source), "output_path": str(output)})
        with patch("app.generators.mafl.load_integrations", return_value=integrations):
            render_mafl([service])

        rendered = yaml.safe_load(output.read_text(encoding="utf-8"))
        item = rendered["services"]["Infrastructure"][0]
        self.assertEqual(item["icon"]["color"], "#e57000")

    def test_mafl_renderer_can_emit_flat_config_shape(self):
        source = Path(self.tmp.name) / "mafl.yml"
        output = Path(self.tmp.name) / "rendered.yml"
        source.write_text(
            yaml.dump(
                {
                    "title": "Middle Earth Labs",
                    "services": {"Infrastructure": [{"title": "Proxmox VE", "link": "https://pve/"}]},
                }
            ),
            encoding="utf-8",
        )
        integrations = IntegrationsConfig(
            mafl={"source_path": str(source), "output_path": str(output), "services_layout": "flat"}
        )
        with patch("app.generators.mafl.load_integrations", return_value=integrations):
            render_mafl([])

        rendered = yaml.safe_load(output.read_text(encoding="utf-8"))
        self.assertIsInstance(rendered["services"], list)
        self.assertEqual(rendered["services"][0]["title"], "Proxmox VE")

    def test_mafl_renderer_indents_sequences_under_keys(self):
        source = Path(self.tmp.name) / "mafl.yml"
        output = Path(self.tmp.name) / "rendered.yml"
        source.write_text(
            yaml.dump(
                {
                    "title": "Middle Earth Labs",
                    "services": {"Infrastructure": [{"title": "Proxmox VE", "link": "https://pve/"}]},
                }
            ),
            encoding="utf-8",
        )
        integrations = IntegrationsConfig(
            mafl={"source_path": str(source), "output_path": str(output), "services_layout": "flat"}
        )
        with patch("app.generators.mafl.load_integrations", return_value=integrations):
            render_mafl([])

        text = output.read_text(encoding="utf-8")
        self.assertIn("services:\n  - ", text)
        self.assertIn("    title: Proxmox VE", text)

    def test_mafl_renderer_can_preserve_grouped_config_shape(self):
        source = Path(self.tmp.name) / "mafl.yml"
        output = Path(self.tmp.name) / "rendered.yml"
        source.write_text(
            yaml.dump(
                {
                    "title": "Middle Earth Labs",
                    "services": {"Infrastructure": [{"title": "Proxmox VE", "link": "https://pve/"}]},
                }
            ),
            encoding="utf-8",
        )
        integrations = IntegrationsConfig(
            mafl={"source_path": str(source), "output_path": str(output), "services_layout": "grouped"}
        )
        with patch("app.generators.mafl.load_integrations", return_value=integrations):
            render_mafl([])

        rendered = yaml.safe_load(output.read_text(encoding="utf-8"))
        self.assertIsInstance(rendered["services"], dict)
        self.assertEqual(rendered["services"]["Infrastructure"][0]["title"], "Proxmox VE")

    def test_mafl_renderer_removes_empty_groups(self):
        source = Path(self.tmp.name) / "mafl.yml"
        output = Path(self.tmp.name) / "rendered.yml"
        source.write_text(
            yaml.dump(
                {
                    "title": "Middle Earth Labs",
                    "services": {
                        "Infrastructure": [{"title": "Proxmox VE", "link": "https://pve/"}],
                        "Media & Automation": [],
                    },
                }
            ),
            encoding="utf-8",
        )
        integrations = IntegrationsConfig(
            mafl={"source_path": str(source), "output_path": str(output), "services_layout": "grouped"}
        )
        with patch("app.generators.mafl.load_integrations", return_value=integrations):
            render_mafl([])

        rendered = yaml.safe_load(output.read_text(encoding="utf-8"))
        self.assertIn("Infrastructure", rendered["services"])
        self.assertNotIn("Media & Automation", rendered["services"])

    def test_mafl_deploy_landing_zone_writes_request_without_ssh(self):
        source = Path(self.tmp.name) / "mafl.yml"
        output = Path(self.tmp.name) / "config.yml"
        source.write_text(yaml.dump({"title": "Middle Earth Labs", "services": {}}), encoding="utf-8")
        service = ServiceRecord(
            **valid_service(
                exposure={"homepage": True, "reverse_proxy": True, "public": False},
            )
        )
        integrations = IntegrationsConfig(
            mafl={
                "source_path": str(source),
                "output_path": str(output),
                "deploy": {
                    "mode": "landing",
                    "nas_path": "/mnt/downloads/mafl/config.yml",
                    "path": "/docker/mafl/config.yml",
                    "restart_command": "docker restart mafl",
                },
            },
        )

        with patch("app.generators.mafl.load_integrations", return_value=integrations):
            import asyncio

            asyncio.run(sync_mafl([service]))

        request = output.with_name("config.deploy.yml")
        request_data = yaml.safe_load(request.read_text(encoding="utf-8"))
        self.assertEqual(request_data["source"], "/mnt/downloads/mafl/config.yml")
        self.assertEqual(request_data["destination"], "/docker/mafl/config.yml")
        self.assertEqual(request_data["restart_command"], "docker restart mafl")

    def test_mafl_deploy_direct_mode_writes_to_live_path_without_request(self):
        source = Path(self.tmp.name) / "mafl.yml"
        landing_output = Path(self.tmp.name) / "landing" / "config.yml"
        live_output = Path(self.tmp.name) / "docker" / "mafl" / "config.yml"
        source.write_text(yaml.dump({"title": "Middle Earth Labs", "services": {}}), encoding="utf-8")
        service = ServiceRecord(
            **valid_service(
                exposure={"homepage": True, "reverse_proxy": True, "public": False},
            )
        )
        integrations = IntegrationsConfig(
            mafl={
                "source_path": str(source),
                "output_path": str(landing_output),
                "deploy": {
                    "mode": "direct",
                    "path": str(live_output),
                },
            },
        )

        with patch("app.generators.mafl.load_integrations", return_value=integrations):
            import asyncio

            asyncio.run(sync_mafl([service]))

        self.assertTrue(live_output.exists())
        self.assertFalse(landing_output.exists())
        self.assertFalse(live_output.with_name("config.deploy.yml").exists())

    def test_mafl_direct_mode_permission_error_mentions_container_mount(self):
        source = Path(self.tmp.name) / "mafl.yml"
        source.write_text(yaml.dump({"title": "Middle Earth Labs", "services": {}}), encoding="utf-8")
        integrations = IntegrationsConfig(
            mafl={
                "source_path": str(source),
                "deploy": {
                    "mode": "direct",
                    "path": "/docker/mafl/config.yml",
                },
            },
        )

        with patch("app.generators.mafl.load_integrations", return_value=integrations):
            with patch.object(Path, "mkdir", side_effect=PermissionError(13, "Permission denied", "/docker")):
                with self.assertRaisesRegex(RuntimeError, "/docker/mafl:/docker/mafl"):
                    render_mafl([])

    def test_mafl_promote_service_quotes_restart_command(self):
        service_text = Path("deploy/mafl-promote.service").read_text(encoding="utf-8")
        self.assertIn('Environment="MAFL_RESTART_COMMAND=docker restart mafl"', service_text)

    def test_render_route_dispatches_config_action(self):
        web.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with patch("app.web.render_mafl") as render:
            resp = self.client.post(
                "/render/mafl",
                data={"csrf_token": web._csrf_token()},
                follow_redirects=False,
            )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("config_action=rendered", resp.headers["location"])
        self.assertIn("config_target=mafl", resp.headers["location"])
        render.assert_called_once()

    def test_sync_mafl_route_dispatches_deploy_action(self):
        web.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with patch("app.web.sync_mafl") as sync:
            resp = self.client.post(
                "/sync/mafl",
                data={"csrf_token": web._csrf_token()},
                follow_redirects=False,
            )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("config_action=deployed", resp.headers["location"])
        self.assertIn("config_target=mafl", resp.headers["location"])
        sync.assert_called_once()


if __name__ == "__main__":
    unittest.main()
