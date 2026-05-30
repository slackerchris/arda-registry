import os
import requests
from typing import List
from app.models import ServiceRecord

def sync_npm(services: List[ServiceRecord]):
    npm_url = os.environ.get("NPM_URL", "").strip().rstrip('/')
    email = os.environ.get("NPM_EMAIL", "").strip()
    password = os.environ.get("NPM_PASSWORD", "").strip()
    
    token = None
    if npm_url and email and password:
        try:
            resp = requests.post(
                f"{npm_url}/api/tokens",
                json={"identity": email, "secret": password},
                timeout=10,
            )
            resp.raise_for_status()
            token = resp.json()["token"]
        except requests.exceptions.RequestException as e:
            print(f"Failed to connect to NPM API at {npm_url}: {e}")
            print("Note: Set NPM_URL, NPM_EMAIL, NPM_PASSWORD environment variables for actual sync.")
            print("Running in Dry Run mode.")
    else:
        print("Note: Set NPM_URL, NPM_EMAIL, NPM_PASSWORD environment variables for actual sync.")
        print("Running in Dry Run mode.")

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    
    existing_hosts = {}
    if token:
        try:
            resp = requests.get(f"{npm_url}/api/nginx/proxy-hosts", headers=headers, timeout=10)
            resp.raise_for_status()
            for host in resp.json():
                for domain in host.get("domain_names", []):
                    existing_hosts[domain] = host
        except requests.exceptions.RequestException as e:
            print(f"Failed to get existing proxy hosts: {e}")
            
    created = updated = skipped = failed = 0
    for svc in services:
        if not svc.exposure.reverse_proxy or not svc.network.service_domains:
            skipped += 1
            continue

        domains = svc.network.service_domains
        primary_domain = svc.network.primary_dns
        target_ip = svc.network.ip
        target_port = svc.backend.port
        target_scheme = svc.backend.scheme

        payload = {
            "domain_names": domains,
            "forward_scheme": target_scheme,
            "forward_host": target_ip,
            "forward_port": target_port,
            "caching_enabled": False,
            "block_exploits": True,
            "allow_websocket_upgrade": True,
            "access_list_id": "0",
            "certificate_id": "0",
            "ssl_forced": svc.exposure.force_ssl,
            "hsts_enabled": False,
            "hsts_subdomains": False,
            "http2_support": False,
            "advanced_config": "",
            "meta": {"letsecure": False, "dns_challenge": False}
        }

        if primary_domain in existing_hosts:
            host_id = existing_hosts[primary_domain]["id"]
            if token:
                try:
                    resp = requests.put(
                        f"{npm_url}/api/nginx/proxy-hosts/{host_id}",
                        json=payload,
                        headers=headers,
                        timeout=10,
                    )
                    resp.raise_for_status()
                    print(f"Updated existing proxy host for {primary_domain} (ID {host_id})")
                    updated += 1
                except requests.exceptions.RequestException as e:
                    print(f"Error updating proxy host for {primary_domain}: {e}")
                    failed += 1
            else:
                print(f"[Dry Run] Would update proxy host for {primary_domain} -> {target_ip}:{target_port}")
                updated += 1
        else:
            if token:
                try:
                    resp = requests.post(
                        f"{npm_url}/api/nginx/proxy-hosts",
                        json=payload,
                        headers=headers,
                        timeout=10,
                    )
                    resp.raise_for_status()
                    print(f"Created new proxy host for {primary_domain}")
                    created += 1
                except requests.exceptions.RequestException as e:
                    print(f"Error creating proxy host for {primary_domain}: {e}")
                    failed += 1
            else:
                print(f"[Dry Run] Would create proxy host for {primary_domain} -> {target_ip}:{target_port}")
                created += 1
                
    if token:
        print(f"Successfully synced {created + updated} proxy hosts to NPM.")
    return {"created": created, "updated": updated, "skipped": skipped, "failed": failed}
