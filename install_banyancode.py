#!/usr/bin/env python3
"""Install the pinned BanyanCode ARM64 package into a new directory."""
import argparse
import base64
import hashlib
import io
import json
from pathlib import Path
import tarfile
from urllib.request import urlopen


VERSION = "26.11.2"
PACKAGE = "banyancode-linux-arm64"
REGISTRY = "https://registry.npmjs.org"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    with urlopen(f"{REGISTRY}/{PACKAGE}/{VERSION}", timeout=20) as response:
        expected = json.load(response)["dist"]["integrity"]
    url = f"{REGISTRY}/{PACKAGE}/-/{PACKAGE}-{VERSION}.tgz"
    with urlopen(url, timeout=90) as response:
        payload = response.read()
    digest = base64.b64encode(hashlib.sha512(payload).digest()).decode("ascii")
    if expected != f"sha512-{digest}":
        raise SystemExit("Package integrity check failed")
    args.directory.mkdir(mode=0o700, parents=True)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        for name in ("banyancode", "banyan-indexer"):
            source = archive.extractfile(f"package/bin/{name}")
            target = args.directory / name
            target.write_bytes(source.read())
            target.chmod(0o700)
    print(args.directory / "banyancode")


if __name__ == "__main__":
    main()
