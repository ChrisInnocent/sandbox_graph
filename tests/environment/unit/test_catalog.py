import pytest

from sandbox_graph.environment.catalog import ProfileCatalog, audited_sources


def test_catalog_resolves_only_fixed_audited_profile_and_architecture():
    catalog = ProfileCatalog.standard()
    profile = catalog.resolve("cubesandbox", "cube.oneclick.v1", "x86_64")
    assert profile.product == "cubesandbox"
    assert profile.source_commits["CubeSandbox"] == audited_sources()["CubeSandbox"]
    with pytest.raises(ValueError, match="unsupported architecture"):
        catalog.resolve("cubesandbox", "cube.oneclick.v1", "riscv64")


def test_catalog_rejects_unreviewed_profile_or_source_commit():
    catalog = ProfileCatalog.standard()
    with pytest.raises(ValueError, match="unsupported profile"):
        catalog.resolve("cubesandbox", "floating.latest", "x86_64")
    with pytest.raises(ValueError, match="source commit"):
        catalog.verify_sources({"CubeSandbox": "0" * 40})


def test_catalog_requires_discovered_architecture_audited_source_set_and_artifact_locks():
    from sandbox_graph.environment.contracts import ArtifactLock, EnvironmentDeclaration

    catalog = ProfileCatalog.standard()
    source = audited_sources()["CubeSandbox"]
    declaration = EnvironmentDeclaration(
        environment_id="cube-a", product="cubesandbox", product_version="1.2.3",
        profile="cube.oneclick.v1", source_set=(f"CubeSandbox={source}",),
        nodes=({"node_id":"n1", "machine_id":"h1", "roles":["compute"],
                "ssh_address":"192.0.2.2", "ssh_user":"bench",
                "known_hosts_ref":"artifact://known-hosts",
                "automation_credential_ref":"secret://ssh/key"},),
        artifacts=(ArtifactLock(name="cube", version="1.2.3", digest="a" * 64),),
    )
    assert catalog.verify_declaration(declaration, {"n1":"x86_64"}).profile == "cube.oneclick.v1"
    with pytest.raises(ValueError, match="architecture discovery"):
        catalog.verify_declaration(declaration, {})
    with pytest.raises(ValueError, match="source commit set"):
        catalog.verify_declaration(declaration.model_copy(update={"source_set": ()}), {"n1":"x86_64"})
