"""Audited product profiles. Registration alone does not mark live verification."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


_ROOT = Path(__file__).resolve().parents[2]
_SOURCE_MANIFEST = _ROOT / "docs" / "source-audit" / "upstream-sources.json"


@dataclass(frozen=True)
class ProductProfile:
    product: str
    profile: str
    roles: tuple[str, ...]
    architectures: tuple[str, ...]
    source_commits: Mapping[str, str]
    proof_level: str = "source_audited"


def audited_sources(path: str | Path = _SOURCE_MANIFEST) -> dict[str, str]:
    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    commits = {}
    for source in manifest.get("sources", []):
        local_path = source.get("local_path", "")
        repo = Path(local_path).name
        commits[repo] = source["commit"]
    required = {"CubeSandbox", "AgentENV", "kuasar-sandbox", "orchestrator"}
    if missing := required - commits.keys():
        raise ValueError(f"audited source manifest missing {sorted(missing)}")
    if any(len(commit) != 40 or any(ch not in "0123456789abcdef" for ch in commit) for commit in commits.values()):
        raise ValueError("source manifest contains invalid commit")
    return commits


class ProfileCatalog:
    def __init__(self, profiles: tuple[ProductProfile, ...]):
        self._profiles = {(p.product, p.profile): p for p in profiles}
        if len(self._profiles) != len(profiles):
            raise ValueError("duplicate product profile")

    @classmethod
    def standard(cls) -> "ProfileCatalog":
        sources = audited_sources()
        return cls((
            ProductProfile("cubesandbox", "cube.oneclick.v1", ("control", "compute"), ("x86_64", "aarch64"), {"CubeSandbox": sources["CubeSandbox"]}),
            ProductProfile("kuasar", "kuasar.native.v1", ("conductor", "proxy", "runtime", "registry", "router", "placer", "store", "cache"), ("x86_64", "aarch64"), {key: sources[key] for key in ("kuasar-sandbox", "orchestrator", "sandboxer", "accelerator", "connector", "guest-runtime")}),
            ProductProfile("agentenv", "agentenv.native.v1", ("server", "scheduler", "gateway"), ("x86_64", "aarch64"), {"AgentENV": sources["AgentENV"]}),
        ))

    def resolve(self, product: str, profile: str, architecture: str) -> ProductProfile:
        try:
            selected = self._profiles[(product, profile)]
        except KeyError as exc:
            raise ValueError(f"unsupported profile: {product}/{profile}") from exc
        if architecture not in selected.architectures:
            raise ValueError(f"unsupported architecture: {architecture}")
        if not selected.source_commits:
            raise ValueError("profile has no audited source commit")
        return selected

    def verify_sources(self, supplied: Mapping[str, str]) -> None:
        known = audited_sources()
        for name, commit in supplied.items():
            if name not in known or known[name] != commit:
                raise ValueError(f"source commit mismatch: {name}")

    def verify_declaration(
        self, declaration, architectures: Mapping[str, str], *, mutation: bool = False
    ) -> ProductProfile:
        """Bind every declared node to one audited profile and exact source set."""
        node_architectures = {node.node_id: architectures.get(node.node_id) for node in declaration.nodes}
        missing = sorted(node for node, arch in node_architectures.items() if not arch)
        if missing:
            raise ValueError(f"architecture discovery required for nodes: {missing}")
        if len(set(node_architectures.values())) != 1:
            raise ValueError("mixed node architectures require separate product profiles")
        selected = self.resolve(declaration.product, declaration.profile, next(iter(node_architectures.values())))
        supplied = dict(item.split("=", 1) for item in declaration.source_set if "=" in item)
        if len(supplied) != len(declaration.source_set):
            raise ValueError("source_set entries must be name=commit")
        expected = dict(selected.source_commits)
        if supplied != expected:
            raise ValueError("source commit set does not match audited profile")
        if mutation and not declaration.artifacts:
            raise ValueError("locked installation artifacts are required for a mutation plan")
        return selected

    def profiles(self) -> tuple[ProductProfile, ...]:
        return tuple(self._profiles[key] for key in sorted(self._profiles))
