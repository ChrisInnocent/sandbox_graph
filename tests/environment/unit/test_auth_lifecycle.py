import pytest

from sandbox_graph.environment.auth import provision_binding, resolve_credential
from sandbox_graph.environment.contracts import AuthBinding
from sandbox_graph.environment.secrets import SecretStore


def binding(mode, version=None, ref="secret://lab/api/v1"):
    return AuthBinding(plane="control_api", mode="api_key", credential_ref=ref,
        credential_version=version, provision=mode, header_carrier="X-API-Key")


def test_reuse_never_generates_or_overwrites_existing_credential(tmp_path):
    store=SecretStore(tmp_path/"secrets"); store.put("lab","api","old",version="v1")
    called=[]
    result=provision_binding(binding("reuse"),store,scope="lab",name="api",factory=lambda:called.append(1) or "new")
    assert result.credential_ref=="secret://lab/api/v1" and not result.created
    assert resolve_credential(result,store,scope="lab")=="old"
    assert called==[]


def test_create_if_absent_persists_once_and_reuses_version_on_reentry(tmp_path):
    store=SecretStore(tmp_path/"secrets"); calls=[]
    first=provision_binding(binding("create_if_absent",version="v7",ref="secret://lab/api/v7"),store,
        scope="lab",name="api",factory=lambda:calls.append(1) or "stable")
    second=provision_binding(binding("create_if_absent",version="v7",ref="secret://lab/api/v7"),store,
        scope="lab",name="api",factory=lambda:calls.append(2) or "wrong")
    assert first.credential_ref==second.credential_ref
    assert first.created and not second.created
    assert resolve_credential(second,store,scope="lab")=="stable"
    assert calls==[1]


def test_rotation_requires_explicit_new_version_and_preserves_old_secret(tmp_path):
    store=SecretStore(tmp_path/"secrets"); store.put("lab","api","old",version="v1")
    with pytest.raises(ValueError,match="explicit new credential_version"):
        provision_binding(binding("rotate"),store,scope="lab",name="api",factory=lambda:"new",previous_ref="secret://lab/api/v1")
    rotated=provision_binding(binding("rotate",version="v2",ref="secret://lab/api/v2"),store,
        scope="lab",name="api",factory=lambda:"new",previous_ref="secret://lab/api/v1")
    assert rotated.previous_ref=="secret://lab/api/v1"
    assert resolve_credential(rotated,store,scope="lab")=="new"
    assert store.resolve("secret://lab/api/v1",scope="lab")=="old"
