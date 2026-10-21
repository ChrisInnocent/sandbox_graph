"""Digest-locked product install registry implementation for environment runners."""
from pathlib import Path
import sys

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sandbox_graph.environment.scripts.product_install import InstallAction, ProductInstallRegistry

__all__ = ["InstallAction", "ProductInstallRegistry"]
