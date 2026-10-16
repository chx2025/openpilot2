from __future__ import annotations

from pathlib import Path

CLUSTER_DIR = Path(__file__).resolve().parent
CLUSTER_ASSETS_DIR = CLUSTER_DIR / "assets"
REPO_ROOT = CLUSTER_DIR.parents[2]


def _openpilot_package_root() -> Path:
    if (REPO_ROOT / "cereal" / "log.capnp").is_file():
        return REPO_ROOT
    # sunnypilot keeps cereal/selfdrive/system under <repo>/openpilot; older layouts keep them at the repo root.
    nested = REPO_ROOT / "openpilot"
    if (nested / "cereal").is_dir():
        return nested
    return REPO_ROOT


OPENPILOT_PACKAGE_ROOT = _openpilot_package_root()
CEREAL_DIR = OPENPILOT_PACKAGE_ROOT / "cereal"
SELFDRIVE_DIR = OPENPILOT_PACKAGE_ROOT / "selfdrive"
OPENDBC_REPO_DIR = REPO_ROOT / "opendbc_repo"
