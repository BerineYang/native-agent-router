from types import SimpleNamespace

from native_agent_router import doctor


class EmptyConfig(SimpleNamespace):
    def agent_ids(self):
        return []


def test_doctor_marks_healthy_dependencies(monkeypatch, capsys, tmp_path):
    cfg = EmptyConfig(config_path=tmp_path / "agents.json", home=tmp_path)
    monkeypatch.setattr(doctor, "load_config", lambda _path: cfg)
    monkeypatch.setattr(doctor, "find_node", lambda: "/tools/node")
    monkeypatch.setattr(doctor, "find_zcode_cjs", lambda: "/tools/zcode.cjs")
    monkeypatch.setattr(
        doctor,
        "find_version",
        lambda argv: "v22.12.0" if len(argv) == 2 else "0.16.5",
    )
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/tools/git" if name == "git" else None)

    doctor.run_doctor()
    output = capsys.readouterr().out

    assert "[OK] python:" in output
    assert "[OK] node: /tools/node (v22.12.0)" in output
    assert "[OK] git: /tools/git" in output
    assert "[OK] config:" in output
    assert "[OK] zcode CLI: /tools/zcode.cjs" in output
    assert "[OK] zcode protocol: 0.16 app-server supported" in output


def test_doctor_marks_missing_optional_adapter_requirements(monkeypatch, capsys, tmp_path):
    cfg = EmptyConfig(config_path=None, home=tmp_path)
    monkeypatch.setattr(doctor, "load_config", lambda _path: cfg)
    monkeypatch.setattr(doctor, "find_node", lambda: None)
    monkeypatch.setattr(doctor, "find_zcode_cjs", lambda: None)
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: None)

    doctor.run_doctor()
    output = capsys.readouterr().out

    assert "[X] node: NOT FOUND" in output
    assert "[X] git: NOT FOUND" in output
    assert "[i] config: none found" in output
    assert "[X] zcode CLI: NOT FOUND" in output
    assert "[i] done. [X] items require attention only when you use the related adapter." in output
