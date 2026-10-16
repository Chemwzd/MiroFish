#!/usr/bin/env python3
"""Minimal, idempotent patch that lets MiroFish talk to a local Zep-compatible
service instead of Zep Cloud.

Three edits, all narrow:

  backend/app/config.py        drop the hard rejection of ZEP_API_URL
  backend/app/utils/zep.py     read the base URL from MIROFISH_ZEP_BASE_URL
                               and drop the ZEP_API_URL guard
  frontend/vite.config.js      proxy to 127.0.0.1 (Node resolves localhost to
                               ::1, Flask listens on IPv4) and do not auto-open

Usage: patch_mirofish.py <repo_root>
"""

from __future__ import annotations

import pathlib
import sys

CONFIG_OLD = """        if os.environ.get("ZEP_API_URL"):
            errors.append("ZEP_API_URL 不受支持；MiroFish 仅连接 Zep Cloud")
"""

CONFIG_NEW = """        # Patched for a local Zep-compatible service (see zepshim).
        # The upstream build rejects ZEP_API_URL outright; the local shim is
        # addressed through MIROFISH_ZEP_BASE_URL instead, which
        # utils/zep.py reads below. Nothing else about this check changes.
"""

ZEP_URL_OLD = 'ZEP_CLOUD_BASE_URL = "https://api.getzep.com/api/v2"\n'

ZEP_URL_NEW = (
    "# Patched: allow a local Zep-compatible service. Defaults to Zep Cloud so\n"
    "# upstream behaviour is preserved when the variable is unset.\n"
    'ZEP_CLOUD_BASE_URL = os.environ.get(\n'
    '    "MIROFISH_ZEP_BASE_URL", "https://api.getzep.com/api/v2"\n'
    ').rstrip("/")\n'
)

ZEP_GUARD_OLD = """    # zep-cloud gives ZEP_API_URL precedence even when base_url is explicit.
    # Reject it so this Cloud-only integration cannot silently target a
    # self-hosted or compatibility endpoint.
    if os.environ.get("ZEP_API_URL"):
        raise ValueError("ZEP_API_URL is unsupported; unset it to use Zep Cloud")
"""

ZEP_GUARD_NEW = """    # Patched: the ZEP_API_URL guard is removed because the configured
    # endpoint is now whatever MIROFISH_ZEP_BASE_URL points at.
"""

VITE_PROXY_OLD = "        target: 'http://localhost:5001'"
VITE_PROXY_NEW = "        target: 'http://127.0.0.1:5001'"

VITE_OPEN_OLD = "    open: true,"
VITE_OPEN_NEW = "    open: false,"


def patch_file(path: pathlib.Path, replacements: list[tuple[str, str, str]]) -> list[str]:
    if not path.exists():
        raise SystemExit(f"missing file: {path}")
    text = path.read_text()
    applied: list[str] = []
    for label, old, new in replacements:
        if new in text and old not in text:
            applied.append(f"{label}: already patched")
            continue
        if old not in text:
            raise SystemExit(f"{path.name}: expected {label} text not found")
        text = text.replace(old, new, 1)
        applied.append(f"{label}: patched")
    path.write_text(text)
    return applied


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    root = pathlib.Path(sys.argv[1]).resolve()
    if not (root / "backend" / "app").is_dir():
        raise SystemExit(f"{root} does not look like a MiroFish checkout")

    for line in patch_file(
        root / "backend" / "app" / "config.py",
        [("config.py ZEP_API_URL check", CONFIG_OLD, CONFIG_NEW)],
    ):
        print("  " + line)

    for line in patch_file(
        root / "backend" / "app" / "utils" / "zep.py",
        [
            ("zep.py base URL", ZEP_URL_OLD, ZEP_URL_NEW),
            ("zep.py ZEP_API_URL guard", ZEP_GUARD_OLD, ZEP_GUARD_NEW),
        ],
    ):
        print("  " + line)

    for line in patch_file(
        root / "frontend" / "vite.config.js",
        [
            ("vite proxy target", VITE_PROXY_OLD, VITE_PROXY_NEW),
            ("vite auto-open", VITE_OPEN_OLD, VITE_OPEN_NEW),
        ],
    ):
        print("  " + line)

    print(f"patched MiroFish at {root}")


if __name__ == "__main__":
    main()
