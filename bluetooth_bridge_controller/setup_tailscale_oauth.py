#!/usr/bin/env python3
import getpass
import subprocess

from tailscale_oauth import CLIENT_ID_SERVICE, CLIENT_SECRET_SERVICE, KEYCHAIN_ACCOUNT


def save(service: str, value: str):
    subprocess.run(
        ["security", "add-generic-password", "-U", "-a", KEYCHAIN_ACCOUNT, "-s", service, "-w", value],
        check=True,
        stdout=subprocess.DEVNULL,
    )


client_id = getpass.getpass("Tailscale OAuth Client ID（输入隐藏）: ").strip()
client_secret = getpass.getpass("Tailscale OAuth Client Secret（输入隐藏）: ").strip()
if not client_id or not client_secret:
    raise SystemExit("Client ID and Client Secret are required")
save(CLIENT_ID_SERVICE, client_id)
save(CLIENT_SECRET_SERVICE, client_secret)
print("Tailscale OAuth credentials saved to macOS Keychain")
