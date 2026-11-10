import base64
import json
import subprocess
import urllib.parse
import urllib.request


KEYCHAIN_ACCOUNT = "bluetooth-bridge-controller"
CLIENT_ID_SERVICE = "com.lumen.bluetoothbridge.tailscale.client-id"
CLIENT_SECRET_SERVICE = "com.lumen.bluetoothbridge.tailscale.client-secret"


def keychain_read(service: str) -> str:
    result = subprocess.run(
        ["security", "find-generic-password", "-a", KEYCHAIN_ACCOUNT, "-s", service, "-w"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def credentials_available() -> bool:
    try:
        return bool(keychain_read(CLIENT_ID_SERVICE) and keychain_read(CLIENT_SECRET_SERVICE))
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _request(url: str, *, method="GET", headers=None, data=None):
    request = urllib.request.Request(url, method=method, headers=headers or {}, data=data)
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def access_token() -> str:
    client_id = keychain_read(CLIENT_ID_SERVICE)
    client_secret = keychain_read(CLIENT_SECRET_SERVICE)
    data = urllib.parse.urlencode({"client_id": client_id, "client_secret": client_secret}).encode()
    response = _request("https://api.tailscale.com/api/v2/oauth/token", method="POST", data=data)
    token = response.get("access_token")
    if not token:
        raise RuntimeError("Tailscale OAuth did not return an access token")
    return token


def create_one_time_auth_key(description: str, tag="tag:wuying", expiry_seconds=3600):
    token = access_token()
    payload = {
        "capabilities": {"devices": {"create": {
            "reusable": False,
            "ephemeral": False,
            "preauthorized": True,
            "tags": [tag],
        }}},
        "expirySeconds": int(expiry_seconds),
        "description": description[:50],
    }
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    return _request(
        "https://api.tailscale.com/api/v2/tailnet/-/keys",
        method="POST",
        headers=headers,
        data=json.dumps(payload).encode("utf-8"),
    )

