import argparse
import asyncio
import sys
from dotenv import load_dotenv
from pydantic import ValidationError
import yaml
from app.registry import Registry

# Load environment variables from .env file
load_dotenv()

from app.generators.mafl import render_mafl, sync_mafl
from app.generators.ansible import render_ansible
from app.generators.npm import sync_npm
from app.generators.monitoring import render_monitoring
from app.integrations import load_integrations

def load_registry_or_exit(data_dir):
    registry = Registry(data_dir)
    registry.load()
    registry.validate_cross_service()
    try:
        load_integrations()
    except (ValidationError, yaml.YAMLError, OSError) as e:
        registry.errors.append(("data/integrations.yml", f"Integration config error: {e}"))
    if registry.errors:
        print(f"Found {len(registry.errors)} errors. Please fix them before proceeding:")
        for path, err in registry.errors:
            print(f"  - {path}: {err}")
        sys.exit(1)
    return registry

def cmd_validate(args):
    registry = load_registry_or_exit(args.data_dir)
    print(f"Found {len(registry.services)} valid services.")
    print("All services valid!")

def main():
    parser = argparse.ArgumentParser(description="Arda Registry CLI")
    parser.add_argument("--data-dir", default="data/services", help="Path to services directory")
    
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    parser_validate = subparsers.add_parser("validate", help="Validate service definitions")
    
    parser_render = subparsers.add_parser("render", help="Render configurations")
    parser_render.add_argument("target", choices=["mafl", "ansible", "monitoring"], help="Target to render")
    
    parser_sync = subparsers.add_parser("sync", help="Sync configurations to external services")
    parser_sync.add_argument("target", choices=["npm", "mafl"], help="Target to sync")

    parser_serve = subparsers.add_parser("serve", help="Launch the web UI")
    parser_serve.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser_serve.add_argument("--port", default=8888, type=int, help="Bind port (default: 8888)")

    args = parser.parse_args()
    
    if args.command == "validate":
        cmd_validate(args)
    elif args.command == "render":
        registry = load_registry_or_exit(args.data_dir)
        if args.target == "mafl":
            render_mafl(registry.services)
        elif args.target == "ansible":
            render_ansible(registry.services)
        elif args.target == "monitoring":
            render_monitoring(registry.services)
    elif args.command == "sync":
        registry = load_registry_or_exit(args.data_dir)
        if args.target == "npm":
            sync_npm(registry.services)
        elif args.target == "mafl":
            asyncio.run(sync_mafl(registry.services))
    elif args.command == "serve":
        import uvicorn
        import app.web as web_module
        from pathlib import Path
        web_module.DATA_DIR = Path(args.data_dir)
        web_module._setup_ops_logger()
        print(f"Starting Arda Registry UI at http://{args.host}:{args.port}")
        if args.host not in {"127.0.0.1", "localhost", "::1"}:
            print("Remote access requires ARDA_UI_PASSWORD or ARDA_UI_TOKEN.")
        uvicorn.run(web_module.app, host=args.host, port=args.port)

if __name__ == "__main__":
    main()
