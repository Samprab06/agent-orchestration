"""Minimal GitHub REST/GraphQL client with pluggable credentials (stdlib only)."""
from __future__ import annotations

import base64
import json
import os
import shutil
import stat
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API = "https://api.github.com"
USER_AGENT = "agentorch"
NETWORK_ERRORS = (URLError, TimeoutError, ConnectionError)


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"GitHub API {status}: {message}")
        self.status = status


def _raw_request(method: str, url: str, token: str, data: Any = None, timeout: int = 20) -> Any:
    body = None if data is None else json.dumps(data).encode("utf-8")
    request = Request(url, data=body, method=method, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    })
    # Never forwarded on redirects to another host.
    request.add_unredirected_header("Authorization", f"Bearer {token}")
    try:
        with urlopen(request, timeout=timeout) as response:
            if response.status == 204:
                return None
            payload = response.read()
            return json.loads(payload) if payload else None
    except HTTPError as error:
        try:
            detail = json.loads(error.read() or b"{}").get("message", error.reason)
        except (ValueError, AttributeError):
            detail = str(error.reason)
        raise GitHubError(error.code, str(detail)) from None


# --- credentials -------------------------------------------------------------

class TokenProvider:
    login: Optional[str] = None

    def token(self) -> str:
        raise NotImplementedError


class StaticToken(TokenProvider):
    def __init__(self, token: str):
        if not token:
            raise ValueError("Empty GitHub token")
        self._token = token

    def token(self) -> str:
        return self._token


class GhCliToken(TokenProvider):
    """Reuse the GitHub CLI login (``gh auth login``)."""

    def __init__(self):
        self._token: Optional[str] = None

    def token(self) -> str:
        if self._token is None:
            gh = shutil.which("gh")
            if not gh:
                raise ValueError("No GitHub token: set GH_TOKEN or install gh and run gh auth login")
            out = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, timeout=15)
            if out.returncode or not out.stdout.strip():
                raise ValueError("gh is not logged in; run gh auth login")
            self._token = out.stdout.strip()
        return self._token


def _b64url(value: bytes) -> bytes:
    return base64.urlsafe_b64encode(value).rstrip(b"=")


def app_jwt(app_id: int, key_path: Path) -> str:
    """Sign a GitHub App JWT with openssl so no third-party crypto is needed."""
    openssl = shutil.which("openssl")
    if not openssl:
        raise ValueError("openssl is required for GitHub App authentication")
    info = os.stat(key_path, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("GitHub App private key must be a regular file")
    if os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise ValueError("GitHub App private key must be owner-only (chmod 600)")
    now = int(time.time())
    header = _b64url(b'{"alg":"RS256","typ":"JWT"}')
    claims = _b64url(json.dumps({"iat": now - 60, "exp": now + 540, "iss": str(app_id)}).encode())
    message = header + b"." + claims
    signature = subprocess.run(
        [openssl, "dgst", "-sha256", "-sign", str(key_path)],
        input=message, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10, check=True,
    ).stdout
    return (message + b"." + _b64url(signature)).decode("ascii")


class AppInstallationToken(TokenProvider):
    """Short-lived installation token scoped to exactly one repository."""

    PERMISSIONS = {"contents": "write", "issues": "write", "pull_requests": "write", "metadata": "read"}

    def __init__(self, repository: str, app_id: int, key_path: Path, api: str = API):
        self.repository = repository
        self.app_id = app_id
        self.key_path = key_path
        self.api = api
        self._installation: Optional[int] = None
        self._token: Optional[str] = None
        self._expires = 0.0
        self.login = None

    def token(self) -> str:
        if self._token and self._expires - time.time() > 300:
            return self._token
        jwt = app_jwt(self.app_id, self.key_path)
        if self.login is None:
            slug = _raw_request("GET", f"{self.api}/app", jwt)["slug"]
            self.login = f"{slug}[bot]"
        if self._installation is None:
            self._installation = int(
                _raw_request("GET", f"{self.api}/repos/{self.repository}/installation", jwt)["id"]
            )
        response = _raw_request(
            "POST", f"{self.api}/app/installations/{self._installation}/access_tokens", jwt,
            {"repositories": [self.repository.split("/", 1)[1]], "permissions": self.PERMISSIONS},
        )
        expires = datetime.fromisoformat(response["expires_at"].replace("Z", "+00:00"))
        if expires <= datetime.now(timezone.utc):
            raise ValueError("GitHub returned an expired installation token")
        self._token = response["token"]
        self._expires = expires.timestamp()
        return self._token


def provider_from_env() -> TokenProvider:
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(name):
            return StaticToken(os.environ[name])
    return GhCliToken()


def provider_for(config: Any, repository: str) -> TokenProvider:
    """Credentials for one repository according to the operator config."""
    if config is None:
        return provider_from_env()
    if config.github_auth == "app":
        return AppInstallationToken(repository, config.app_id, config.private_key)
    if config.github_auth == "gh":
        return GhCliToken()
    token = os.environ.get(config.token_env)
    if not token and config.token_file.is_file():
        info = config.token_file.stat()
        if os.name == "posix" and info.st_mode & 0o077:
            raise ValueError(f"{config.token_file} must be owner-only (chmod 600)")
        token = config.token_file.read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError(f"Put a GitHub token in {config.token_file} (chmod 600) or set {config.token_env}")
    return StaticToken(token)


# --- client -------------------------------------------------------------------

class GitHub:
    """Repository-scoped API client."""

    def __init__(self, repository: str, credentials: TokenProvider, api: str = API):
        self.repository = repository
        self.credentials = credentials
        self.api = api
        self._login: Optional[str] = None

    def request(self, method: str, path: str, data: Any = None, params: Optional[Dict[str, Any]] = None) -> Any:
        """``path`` beginning with ``/`` is relative to the repository; ``~`` to the API root."""
        if path.startswith("~"):
            url = self.api + path[1:]
        else:
            url = f"{self.api}/repos/{self.repository}{path}"
        if params:
            url += ("&" if "?" in url else "?") + urlencode(params)
        return _raw_request(method, url, self.credentials.token(), data)

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params=params or None)

    def paginate(self, path: str, max_pages: int = 20, **params: Any) -> Iterator[Dict[str, Any]]:
        params.setdefault("per_page", 100)
        for page in range(1, max_pages + 1):
            items = self.get(path, page=page, **params)
            yield from items
            if len(items) < params["per_page"]:
                return

    def graphql(self, query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
        result = _raw_request("POST", f"{self.api}/graphql", self.credentials.token(),
                              {"query": query, "variables": variables})
        if result.get("errors"):
            raise GitHubError(200, "; ".join(e.get("message", "?") for e in result["errors"]))
        return result["data"]

    def login(self) -> str:
        """The login this client posts as."""
        if self._login:
            return self._login
        self.credentials.token()
        login = getattr(self.credentials, "login", None)
        if not login:
            login = self.get("~/user")["login"]
        self._login = login
        return login

    # Convenience wrappers ---------------------------------------------------
    def issue(self, number: int) -> Dict[str, Any]:
        return self.get(f"/issues/{number}")

    def comments(self, number: int, **params: Any) -> List[Dict[str, Any]]:
        return list(self.paginate(f"/issues/{number}/comments", **params))

    def comment(self, number: int, body: str) -> Dict[str, Any]:
        return self.request("POST", f"/issues/{number}/comments", {"body": body})

    def add_labels(self, number: int, *labels: str) -> None:
        self.request("POST", f"/issues/{number}/labels", {"labels": list(labels)})

    def remove_label(self, number: int, label: str) -> None:
        from urllib.parse import quote
        try:
            self.request("DELETE", f"/issues/{number}/labels/{quote(label, safe='')}")
        except GitHubError as error:
            if error.status != 404:
                raise

    def ensure_label(self, name: str, color: str, description: str = "") -> bool:
        from urllib.parse import quote
        try:
            self.get(f"/labels/{quote(name, safe='')}")
            return False
        except GitHubError as error:
            if error.status != 404:
                raise
        self.request("POST", "/labels", {"name": name, "color": color, "description": description})
        return True


def git_auth_env(token: str, base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Environment that authenticates git to github.com without putting the token in argv or URLs."""
    auth = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env = dict(base if base is not None else {"PATH": os.defpath, "HOME": str(Path.home())})
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {auth}",
    })
    return env
