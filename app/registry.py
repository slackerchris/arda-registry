import yaml
from typing import List, Tuple
from pathlib import Path
from pydantic import ValidationError

from app.models import ServiceRecord

class Registry:
    def __init__(self, data_dir: str = "data/services"):
        self.data_dir = Path(data_dir)
        self.services: List[ServiceRecord] = []
        self.errors: List[Tuple[str, str]] = [] # file path/identifier, error message

    def load(self):
        self.services = []
        self.errors = []
        
        if not self.data_dir.exists():
            self.errors.append((str(self.data_dir), "Directory does not exist"))
            return

        all_files = sorted(
            {p for pat in ("*.yml", "*.yaml") for p in self.data_dir.rglob(pat)}
        )
        for filepath in all_files:
            with open(filepath, "r") as f:
                try:
                    data = yaml.safe_load(f)
                    if not data:
                        self.errors.append((str(filepath), "File is empty"))
                        continue
                    service = ServiceRecord(**data)
                    self.services.append(service)
                except yaml.YAMLError as e:
                    self.errors.append((str(filepath), f"YAML Error: {e}"))
                except ValidationError as e:
                    self.errors.append((str(filepath), f"Validation Error: {e}"))
                except Exception as e:
                    self.errors.append((str(filepath), f"Error: {e}"))

    def validate_cross_service(self):
        slugs = set()
        host_ports = set()
        
        for svc in self.services:
            if svc.slug in slugs:
                self.errors.append((svc.slug, "Duplicate slug"))
            slugs.add(svc.slug)
            
            host_port = f"{svc.network.ip}:{svc.backend.port}"
            if host_port in host_ports:
                self.errors.append((svc.slug, f"Duplicate host:port combination {host_port}"))
            host_ports.add(host_port)
