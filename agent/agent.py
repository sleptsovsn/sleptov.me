#!/usr/bin/env python3
import os
import time
import json
import subprocess
import requests
import socket

CONTROLLER = os.getenv("CONTROLLER")
TOKEN = os.getenv("TOKEN")
AGENT_ID = os.getenv("AGENT_ID", socket.gethostname())
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "20"))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "3600"))

def run(cmd):
    try:
        out = subprocess.check_output(cmd, shell=True, stderr=subprocess.STDOUT)
        return out.decode()
    except subprocess.CalledProcessError as e:
        return e.output.decode()

def detect_os():
    if os.path.exists("/usr/bin/apt"):
        return "debian"
    if os.path.exists("/usr/bin/yum") or os.path.exists("/usr/bin/dnf"):
        return "rhel"
    return "unknown"

def check_updates():
    os_type = detect_os()
    if os_type == "debian":
        run("apt update")
        upgrades = run("apt list --upgradable")
    elif os_type == "rhel":
        upgrades = run("yum check-update")
    else:
        upgrades = "unknown OS"
    return {"agent": AGENT_ID, "type": "check", "os": os_type, "upgrades": upgrades}

def install_updates():
    os_type = detect_os()
    if os_type == "debian":
        out = run("apt upgrade -y")
        reboot_needed = os.path.exists("/var/run/reboot-required")
    elif os_type == "rhel":
        out = run("yum update -y")
        reboot_needed = "kernel" in out.lower()
    else:
        out = "unknown OS"
        reboot_needed = False

    return {"agent": AGENT_ID, "type": "install", "output": out, "reboot": reboot_needed}

def post_report(data):
    try:
        requests.post(
            f"{CONTROLLER}/api/report",
            json=data,
            headers={"Authorization": f"Bearer {TOKEN}"},
            timeout=10,
        )
    except Exception as e:
        print("Report error:", e)

def poll_commands():
    try:
        r = requests.get(
            f"{CONTROLLER}/api/commands?agent_id={AGENT_ID}",
            headers={"Authorization": f"Bearer {TOKEN}"},
            timeout=10,
        )
        return r.json()
    except Exception as e:
        print("Poll error:", e)
        return []

def main():
    last_check = 0
    while True:
        now = time.time()

        if now - last_check > CHECK_INTERVAL:
            report = check_updates()
            post_report(report)
            last_check = now

        cmds = poll_commands()
        for cmd in cmds:
            if cmd == "install":
                result = install_updates()
                post_report(result)
                if result.get("reboot"):
                    run("reboot")

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
