#!/usr/bin/env python3

import argparse
import subprocess
import os
import json
from datetime import datetime, timezone
import shutil
import re

KUBECONFIG = os.path.expanduser("~/.kube/config")
DEFAULT_OUTPUT_DIR = f"debug-output-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
OUTPUT_DIR = DEFAULT_OUTPUT_DIR

def run_cmd(cmd, shell=False):
    print(f"[RUNNING] {' '.join(cmd) if isinstance(cmd, list) else cmd}")
    try:
        result = subprocess.run(cmd, shell=shell, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Command failed: {' '.join(cmd)}\n{e.stderr.strip()}")
        return f"ERROR: {e.stderr.strip()}"

def save_text(text, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)

def extract_id(raw):
    if isinstance(raw, dict):
        return raw.get("id")
    elif isinstance(raw, str):
        match = re.search(r"\(([a-f0-9\-]{36})\)", raw)
        return match.group(1) if match else raw.strip()
    return None

def check_prerequisites(namespace):
    print("[INFO] Checking prerequisites...")
    if not os.path.exists(KUBECONFIG):
        print(f"[ERROR] Kubeconfig not found at {KUBECONFIG}.")
        exit(1)

    result = run_cmd(["kubectl", "config", "current-context"])
    if "ERROR" in result:
        print("[ERROR] Unable to access Kubernetes context.")
        exit(1)

    result = run_cmd(["kubectl", "get", "ns", namespace])
    if "NotFound" in result or "Error" in result or "ERROR" in result:
        print(f"[ERROR] Namespace '{namespace}' is not accessible.")
        exit(1)

    print("[OK] Prerequisites met.")

def check_openstack_auth():
    print("[INFO] Checking OpenStack authentication...")
    test_cmd = run_cmd(["openstack", "token", "issue"])
    if "ERROR" in test_cmd or "Missing" in test_cmd:
        print("[ERROR] OpenStack CLI is not authenticated. Please source your adminrc.")
        exit(1)
    print("[OK] OpenStack authentication validated.")

def collect_health_checks():
    os.makedirs(f"{OUTPUT_DIR}/health", exist_ok=True)
    cmds = {
        "compute_services": ["openstack", "compute", "service", "list"],
        "resource_providers": ["openstack", "resource", "provider", "list"],
        "network_agents": ["openstack", "network", "agent", "list"],
        "volume_services": ["openstack", "volume", "service", "list"],
    }

    for name, cmd in cmds.items():
        output = run_cmd(cmd)
        save_text(output, f"{OUTPUT_DIR}/health/{name}.txt")

def collect_pod_logs(namespace, service_name_contains):
    print(f"[INFO] Collecting logs for: {service_name_contains}")
    pods_output = run_cmd(["kubectl", "get", "pods", "-n", namespace, "-o", "json"])
    try:
        pods = json.loads(pods_output)
    except json.JSONDecodeError:
        print("[ERROR] Failed to parse pod JSON")
        return

    matched_pods = [p for p in pods['items'] if service_name_contains in p['metadata']['name'].lower()]
    for pod in matched_pods:
        pod_name = pod['metadata']['name']
        containers = [c['name'] for c in pod['spec'].get('containers', [])]
        for container in containers:
            log = run_cmd(["kubectl", "logs", pod_name, "-n", namespace, "-c", container])
            save_text(log, f"{OUTPUT_DIR}/logs/{service_name_contains}_{pod_name}_{container}.log")

            log_prev = run_cmd(["kubectl", "logs", pod_name, "-n", namespace, "-c", container, "--previous"])
            if "ERROR" not in log_prev:
                save_text(log_prev, f"{OUTPUT_DIR}/logs/{service_name_contains}_{pod_name}_{container}_previous.log")

        desc = run_cmd(["kubectl", "describe", "pod", pod_name, "-n", namespace])
        save_text(desc, f"{OUTPUT_DIR}/describe/{service_name_contains}_{pod_name}.txt")

def collect_namespace_events(namespace):
    events = run_cmd(["kubectl", "get", "events", "-n", namespace, "--sort-by=.lastTimestamp"])
    save_text(events, f"{OUTPUT_DIR}/events/{namespace}_events.txt")

def collect_nova_info(vm_id):
    os.makedirs(f"{OUTPUT_DIR}/nova", exist_ok=True)

    info_text = run_cmd(["openstack", "server", "show", vm_id])
    save_text(info_text, f"{OUTPUT_DIR}/nova/server_show.txt")

    events = run_cmd(["openstack", "server", "event", "list", vm_id])
    save_text(events, f"{OUTPUT_DIR}/nova/server_events.txt")

    migrations = run_cmd(["openstack", "server", "migration", "list", "--server", vm_id])
    save_text(migrations, f"{OUTPUT_DIR}/nova/migrations.txt")

    try:
        return json.loads(run_cmd(["openstack", "server", "show", vm_id, "-f", "json"]))
    except Exception as e:
        print(f"[WARN] Failed to parse VM details: {e}")
        return {}

def collect_ports_for_vm(vm_id):
    os.makedirs(f"{OUTPUT_DIR}/neutron", exist_ok=True)
    ports_raw = run_cmd(["openstack", "port", "list", "--device-id", vm_id])
    save_text(ports_raw, f"{OUTPUT_DIR}/neutron/vm_ports.txt")

    try:
        ports = json.loads(run_cmd(["openstack", "port", "list", "--device-id", vm_id, "-f", "json"]))
        for port in ports:
            port_id = port.get("ID")
            if port_id:
                port_detail = run_cmd(["openstack", "port", "show", port_id])
                save_text(port_detail, f"{OUTPUT_DIR}/neutron/port_{port_id}.txt")

            network_id = port.get("Network ID")
            if network_id:
                net_detail = run_cmd(["openstack", "network", "show", network_id])
                save_text(net_detail, f"{OUTPUT_DIR}/neutron/network_{network_id}.txt")

    except Exception as e:
        print(f"[WARN] Failed to process VM ports or networks: {e}")

def collect_security_groups_for_vm(vm_id):
    os.makedirs(f"{OUTPUT_DIR}/neutron", exist_ok=True)
    try:
        ports = json.loads(run_cmd(["openstack", "port", "list", "--device-id", vm_id, "-f", "json"]))
        sg_ids = set()

        for port in ports:
            sgs = port.get("Security Group") or port.get("Security Groups")
            if isinstance(sgs, list):
                sg_ids.update(sgs)
            elif isinstance(sgs, str) and sgs.startswith("["):
                sg_ids.update(json.loads(sgs))

        for sg_id in sg_ids:
            print(f"[INFO] Fetching security group: {sg_id}")
            sg_detail = run_cmd(["openstack", "security", "group", "show", sg_id])
            sg_rules = run_cmd(["openstack", "security", "group", "rule", "list", sg_id])
            save_text(sg_detail, f"{OUTPUT_DIR}/neutron/security_group_{sg_id}.txt")
            save_text(sg_rules, f"{OUTPUT_DIR}/neutron/security_group_{sg_id}_rules.txt")

    except Exception as e:
        print(f"[WARN] Failed to collect security group info: {e}")

def collect_volumes_for_vm(vm_id):
    os.makedirs(f"{OUTPUT_DIR}/cinder", exist_ok=True)
    try:
        vm_json = json.loads(run_cmd(["openstack", "server", "show", vm_id, "-f", "json"]))
        attached_vols = vm_json.get("os-extended-volumes:volumes_attached", [])
        save_text(json.dumps(attached_vols, indent=2), f"{OUTPUT_DIR}/cinder/attached_volumes.txt")

        for vol in attached_vols:
            vol_id = vol.get("id")
            if vol_id:
                vol_detail = run_cmd(["openstack", "volume", "show", vol_id])
                save_text(vol_detail, f"{OUTPUT_DIR}/cinder/volume_{vol_id}.txt")

    except Exception as e:
        print(f"[WARN] Failed to collect volumes for VM: {e}")

def collect_stack_info(stack_id):
    os.makedirs(f"{OUTPUT_DIR}/heat", exist_ok=True)

    stack_show = run_cmd(["openstack", "stack", "show", stack_id])
    save_text(stack_show, f"{OUTPUT_DIR}/heat/stack_show.txt")

    resource_list_raw = run_cmd(["openstack", "stack", "resource", "list", stack_id])
    save_text(resource_list_raw, f"{OUTPUT_DIR}/heat/stack_resources.txt")

    try:
        resources = json.loads(run_cmd(["openstack", "stack", "resource", "list", stack_id, "-f", "json"]))
        for res in resources:
            res_name = res.get("resource_name")
            if res_name:
                res_show = run_cmd(["openstack", "stack", "resource", "show", stack_id, res_name])
                save_text(res_show, f"{OUTPUT_DIR}/heat/resource_{res_name}.txt")
    except Exception as e:
        print(f"[WARN] Could not parse Heat resource list: {e}")

def collect_image_and_flavor(vm_data):
    image_id = extract_id(vm_data.get("image"))
    flavor_id = extract_id(vm_data.get("flavor"))
    print(f"[DEBUG] image_id = {image_id}, flavor_id = {flavor_id}")

    if image_id:
        image = run_cmd(["openstack", "image", "show", image_id])
        save_text(image, f"{OUTPUT_DIR}/glance/image_show.txt")
    if flavor_id:
        flavor = run_cmd(["openstack", "flavor", "show", flavor_id])
        save_text(flavor, f"{OUTPUT_DIR}/nova/flavor_show.txt")

def collect_keystone_user_info(user_id_or_name):
    os.makedirs(f"{OUTPUT_DIR}/keystone", exist_ok=True)
    user_info = run_cmd(["openstack", "user", "show", user_id_or_name])
    save_text(user_info, f"{OUTPUT_DIR}/keystone/user_show.txt")

    role_assignments = run_cmd(["openstack", "role", "assignment", "list", "--user", user_id_or_name, "--names"])
    save_text(role_assignments, f"{OUTPUT_DIR}/keystone/user_role_assignments.txt")

def archive_output():
    zip_path = shutil.make_archive(OUTPUT_DIR, 'zip', OUTPUT_DIR)
    print(f"[DONE] Output archived at: {zip_path}")

def main():
    global OUTPUT_DIR
    parser = argparse.ArgumentParser(description="OpenStack Pod Debug Tool")
    parser.add_argument("--namespace", required=True, help="Kubernetes namespace")
    parser.add_argument("--output", default=DEFAULT_OUTPUT_DIR, help="Output directory")
    parser.add_argument("--vm", help="VM ID")
    parser.add_argument("--network", help="Network ID")
    parser.add_argument("--port", help="Port ID")
    parser.add_argument("--volume", help="Volume ID")
    parser.add_argument("--zip", action="store_true", help="Zip output")
    parser.add_argument("--stack", help="Heat Stack ID")
    parser.add_argument("--user", help="Keystone User ID or Name")

    args = parser.parse_args()
    OUTPUT_DIR = args.output
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    check_prerequisites(args.namespace)
    check_openstack_auth()
    collect_health_checks()
    collect_namespace_events(args.namespace)

    vm_data = {}
    if args.vm:
        vm_data = collect_nova_info(args.vm)
        collect_image_and_flavor(vm_data)
        collect_ports_for_vm(args.vm)
        collect_volumes_for_vm(args.vm)
        collect_security_groups_for_vm(args.vm)
        for comp in ["nova", "glance", "image", "keystone", "neutron", "cinder"]:
            collect_pod_logs(args.namespace, comp)

    if args.network:
        collect_pod_logs(args.namespace, "neutron")

    if args.port:
        collect_pod_logs(args.namespace, "neutron")

    if args.volume:
        collect_pod_logs(args.namespace, "cinder")

    if args.stack:
        collect_stack_info(args.stack)
        collect_pod_logs(args.namespace, "heat")

    if args.user:
        collect_keystone_user_info(args.user)
        collect_pod_logs(args.namespace, "keystone")

    summary = f"""Debug Summary - {datetime.now(timezone.utc).isoformat()} UTC\nNamespace: {args.namespace}"""
    save_text(summary, f"{OUTPUT_DIR}/summary.txt")

    if args.zip:
        archive_output()

if __name__ == "__main__":
    main()
