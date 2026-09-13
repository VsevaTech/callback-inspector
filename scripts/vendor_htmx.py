"""Download htmx from the npm registry into app/static so the UI has no runtime CDN dependency.

Run once for local development (``python scripts/vendor_htmx.py``); the Dockerfile runs it at build time.
If the file is missing the UI falls back to jsDelivr, so this is an optimisation, not a hard requirement.
"""

from __future__ import annotations

import io
import sys
import tarfile
import urllib.request
from pathlib import Path

HTMX_VERSION = "2.0.4"
TARBALL = f"https://registry.npmjs.org/htmx.org/-/htmx.org-{HTMX_VERSION}.tgz"
TARGET = Path(__file__).resolve().parents[1] / "app" / "static" / "htmx.min.js"


def main() -> int:
    if TARGET.exists() and TARGET.stat().st_size > 0:
        print(f"already present: {TARGET}")
        return 0
    print(f"downloading {TARBALL}")
    with urllib.request.urlopen(TARBALL, timeout=60) as resp:
        data = resp.read()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        member = tar.extractfile("package/dist/htmx.min.js")
        if member is None:
            print("htmx.min.js not found in tarball", file=sys.stderr)
            return 1
        TARGET.parent.mkdir(parents=True, exist_ok=True)
        TARGET.write_bytes(member.read())
    print(f"saved {TARGET} ({TARGET.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
