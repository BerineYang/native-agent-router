import pytest

from native_agent_router.config import find_zcode_cjs, load_config


def test_unknown_agent_clear_error(home, tmp_path):
    from conftest import make_kernel

    k = make_kernel(home, {"w1": {"adapter": "stub"}})
    ws = tmp_path / "ws-for-agent-test"
    ws.mkdir()
    with pytest.raises(Exception, match="unknown agent_id"):
        k.submit("nope", "g", str(ws))


def test_disabled_agent_rejected(home):
    from conftest import make_kernel

    k = make_kernel(home, {"w1": {"adapter": "stub", "enabled": False}})
    with pytest.raises(Exception, match="disabled"):
        k.submit("w1", "g", str(home))


def test_explicit_command_resolution(home, tmp_path):
    from native_agent_router.config import Config

    c = Config({"agents": {"acp1": {"adapter": "acp-generic", "command": ["fake-acp", "serve"]}}}, None, home)
    spec = c.agent_spec("acp1")
    assert spec["argv"] == ["fake-acp", "serve"]
    assert spec["adapter"] == "acp-generic"


def test_zcode_discovery_via_env(home, tmp_path, monkeypatch):
    pytest.importorskip("shutil")
    from native_agent_router.config import find_node

    if not find_node():
        pytest.skip("node not installed")
    fake_cjs = tmp_path / "zcode.cjs"
    fake_cjs.write_text("//fake", encoding="utf-8")
    monkeypatch.setenv("ZCODE_BIN", str(fake_cjs))
    found = find_zcode_cjs()
    assert found and found.endswith("zcode.cjs")
    from native_agent_router.config import Config

    c = Config({"agents": {"zcode": {"adapter": "zcode-native"}}}, None, home)
    spec = c.agent_spec("zcode")
    assert spec["argv"][0] == find_node()
    assert spec["argv"][-1] == "app-server"


def test_config_file_loading(home):
    import json
    import os

    cfg_path = home / "agents.json"
    home.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({"agents": {"s1": {"adapter": "stub", "scenario": []}}}), encoding="utf-8")
    c = load_config()
    assert c.config_path == cfg_path
    assert "s1" in c.agent_ids()
