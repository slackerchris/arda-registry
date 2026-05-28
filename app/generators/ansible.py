import yaml
from pathlib import Path
from typing import List
from app.models import ServiceRecord

def render_ansible(services: List[ServiceRecord], output_path: str = "output/ansible/inventory.yml"):
    inventory = {
        "all": {
            "children": {},
            "hosts": {}
        }
    }
    
    count = 0
    for svc in services:
        if not svc.active:
            continue
        vlan = svc.network.vlan
        
        if vlan not in inventory["all"]["children"]:
            inventory["all"]["children"][vlan] = {"hosts": {}}
            
        host_vars = {
            "ansible_host": svc.network.ip,
            "service_slug": svc.slug,
            "vlan": vlan
        }
        
        if svc.runtime:
            host_vars["runtime_type"] = svc.runtime.type
            host_vars["pve_host"] = svc.runtime.host
            if svc.runtime.container_id:
                host_vars["lxc_id"] = svc.runtime.container_id
                
        inventory["all"]["children"][vlan]["hosts"][svc.slug] = host_vars
        inventory["all"]["hosts"][svc.slug] = host_vars
        count += 1
        
    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    
    with open(output_file, "w") as f:
        yaml.dump(inventory, f, default_flow_style=False, sort_keys=False)
        
    print(f"Rendered Ansible inventory to {output_path} with {count} hosts.")
