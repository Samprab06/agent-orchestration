"""Mint repository-scoped GitHub App credentials in memory."""
import base64
from datetime import datetime, timezone
import json
import os
import stat
import subprocess
import time
from urllib.request import Request, urlopen


API = "https://api.github.com"
PERMISSIONS = {"contents": "write", "issues": "write", "pull_requests": "write"}


def request_json(method, path, credential, data=None):
    body = None if data is None else json.dumps(data).encode("utf-8")
    request = Request(API + path, data=body, method=method, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "hackathon-agent-orchestrator",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    })
    request.add_unredirected_header("Authorization", f"Bearer {credential}")
    with urlopen(request, timeout=20) as response:
        return None if response.status == 204 else json.load(response)


def encode(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=")


def app_jwt(app_id, key_path):
    now = int(time.time())
    header = encode(b'{"alg":"RS256","typ":"JWT"}')
    claims = encode(json.dumps({"iat": now - 60, "exp": now + 540, "iss": str(app_id)}).encode())
    message = header + b"." + claims
    fd = os.open(key_path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("GitHub App private key must be an owner-only regular file")
        signature = subprocess.run(
            ["/usr/bin/openssl", "dgst", "-sha256", "-sign", f"/proc/self/fd/{fd}",
             "-passin", "pass:"],
            input=message, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            pass_fds=(fd,), timeout=10, check=True,
        ).stdout
    finally:
        os.close(fd)
    return (message + b"." + encode(signature)).decode("ascii")


class InstallationCredentials:
    def __init__(self, repository, app_id, key_path):
        self.repository = repository
        self.app_id = app_id
        self.key_path = key_path
        self.installation_id = None
        self._token = None
        self._expires = 0

    def token(self):
        if self._token and self._expires - time.time() > 300:
            return self._token
        jwt = app_jwt(self.app_id, self.key_path)
        if self.installation_id is None:
            installation = request_json(
                "GET", f"/repos/{self.repository}/installation", jwt
            )
            self.installation_id = int(installation["id"])
        response = request_json(
            "POST", f"/app/installations/{self.installation_id}/access_tokens", jwt,
            {"repositories": [self.repository.split("/", 1)[1]], "permissions": PERMISSIONS},
        )
        token = response["token"]
        access = request_json("GET", "/installation/repositories", token)
        names = [repo["full_name"].casefold() for repo in access["repositories"]]
        if access["total_count"] != 1 or names != [self.repository.casefold()]:
            raise ValueError("GitHub App token is not scoped to the requested repository")
        expires = datetime.fromisoformat(response["expires_at"].replace("Z", "+00:00"))
        if expires.tzinfo is None or expires <= datetime.now(timezone.utc):
            raise ValueError("GitHub returned an invalid installation token expiry")
        self._expires = expires.timestamp()
        self._token = token
        return token
