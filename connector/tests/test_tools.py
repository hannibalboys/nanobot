"""Tests for the client tool registry: validation, rendering, credentials (v2)."""

from __future__ import annotations

import json

import pytest

from nanobot_connector.persistence import LocalStateConflictError, LocalStateError
from nanobot_connector.tools import (
    InvalidArgsError,
    MissingCredentialError,
    SecretStore,
    ToolDef,
    ToolNotFoundError,
    ToolRegistry,
)


def _echo_tool(**overrides) -> ToolDef:
    base = {
        "name": "say",
        "exec": "/bin/echo",
        "params": [{"name": "msg", "type": "string", "required": True}],
        "argv": ["{msg}"],
        "approval": "auto",
    }
    base.update(overrides)
    return ToolDef.model_validate(base)


def test_render_simple_argv():
    reg = ToolRegistry([_echo_tool()])
    argv, env = reg.render("say", {"msg": "hello"})
    assert argv == ["/bin/echo", "hello"]
    assert env == {}


def test_unknown_tool_raises():
    reg = ToolRegistry([])
    with pytest.raises(ToolNotFoundError):
        reg.render("nope", {})


def test_unknown_argument_rejected():
    reg = ToolRegistry([_echo_tool()])
    with pytest.raises(InvalidArgsError):
        reg.render("say", {"msg": "hi", "surprise": "x"})


def test_missing_required_argument_rejected():
    reg = ToolRegistry([_echo_tool()])
    with pytest.raises(InvalidArgsError):
        reg.render("say", {})


def test_shell_metacharacters_stay_one_argv_element():
    """The whole point of no-shell exec: `; rm -rf` is a literal single arg."""
    reg = ToolRegistry([_echo_tool()])
    argv, _ = reg.render("say", {"msg": "; rm -rf /"})
    assert argv == ["/bin/echo", "; rm -rf /"]


def test_enum_constraint():
    tool = _echo_tool(
        params=[{"name": "mode", "type": "enum", "required": True, "choices": ["a", "b"]}],
        argv=["--mode", "{mode}"],
    )
    reg = ToolRegistry([tool])
    assert reg.render("say", {"mode": "a"})[0] == ["/bin/echo", "--mode", "a"]
    with pytest.raises(InvalidArgsError):
        reg.render("say", {"mode": "z"})


def test_int_bounds():
    tool = _echo_tool(
        params=[{"name": "n", "type": "int", "required": True, "min": 1, "max": 10}],
        argv=["-n", "{n}"],
    )
    reg = ToolRegistry([tool])
    assert reg.render("say", {"n": 5})[0] == ["/bin/echo", "-n", "5"]
    with pytest.raises(InvalidArgsError):
        reg.render("say", {"n": 99})
    with pytest.raises(InvalidArgsError):
        reg.render("say", {"n": "not-a-number"})


def test_string_pattern():
    tool = _echo_tool(
        params=[{"name": "msg", "type": "string", "required": True, "pattern": r"[a-z]+"}],
    )
    reg = ToolRegistry([tool])
    assert reg.render("say", {"msg": "abc"})[0] == ["/bin/echo", "abc"]
    with pytest.raises(InvalidArgsError):
        reg.render("say", {"msg": "ABC123"})


def test_path_param_containment(tmp_path):
    allowed = tmp_path / "docs"
    allowed.mkdir()
    inside = allowed / "f.txt"
    inside.write_text("x", encoding="utf-8")
    tool = _echo_tool(
        params=[{"name": "p", "type": "path", "required": True, "allowedDir": str(allowed)}],
        argv=["{p}"],
    )
    reg = ToolRegistry([tool])
    argv, _ = reg.render("say", {"p": str(inside)})
    assert argv[1] == str(inside.resolve())
    with pytest.raises(InvalidArgsError):
        reg.render("say", {"p": str(tmp_path / "outside.txt")})


def test_optional_token_dropped_when_arg_omitted():
    tool = _echo_tool(
        params=[
            {"name": "msg", "type": "string", "required": True},
            {"name": "flag", "type": "string", "required": False},
        ],
        argv=["{msg}", "--extra={flag}"],
    )
    reg = ToolRegistry([tool])
    # flag omitted → the --extra token disappears entirely
    assert reg.render("say", {"msg": "hi"})[0] == ["/bin/echo", "hi"]
    # flag provided → substituted as a single element
    assert reg.render("say", {"msg": "hi", "flag": "v"})[0] == ["/bin/echo", "hi", "--extra=v"]


def test_secret_injected_from_store_not_protocol(tmp_path):
    store = SecretStore(tmp_path / "secrets.json")
    store.set("api-id", "s3cr3t")
    tool = _echo_tool(secrets={"API_TOKEN": "api-id"})
    reg = ToolRegistry([tool], secrets=store)
    _argv, env = reg.render("say", {"msg": "hi"})
    assert env["API_TOKEN"] == "s3cr3t"
    # public schema exposes only the var name, never the value
    assert tool.public()["secretVars"] == ["API_TOKEN"]
    assert "s3cr3t" not in json.dumps(tool.public())


def test_missing_credential_raises(tmp_path):
    tool = _echo_tool(secrets={"API_TOKEN": "absent-id"})
    reg = ToolRegistry([tool], secrets=SecretStore(tmp_path / "secrets.json"))
    with pytest.raises(MissingCredentialError):
        reg.render("say", {"msg": "hi"})


def test_persistence_roundtrip(tmp_path):
    path = tmp_path / "tools.json"
    reg = ToolRegistry([_echo_tool()], path=path)
    reg.save()
    assert path.exists()
    reloaded = ToolRegistry.load(path=path)
    assert [t.name for t in reloaded.list()] == ["say"]


def test_save_rejects_stale_registry_snapshot(tmp_path):
    path = tmp_path / "tools.json"
    first = ToolRegistry.load(path=path)
    second = ToolRegistry.load(path=path)

    first.add(_echo_tool())
    first.save()
    second.add(ToolDef(name="other", exec="echo"))

    with pytest.raises(LocalStateConflictError, match="其他连接器进程"):
        second.save()
    assert [tool.name for tool in ToolRegistry.load(path=path).list()] == ["say"]


def test_public_schema_hides_internals():
    tool = _echo_tool(env={"FOO": "bar"}, secrets={"TOK": "id"})
    pub = tool.public()
    assert pub["name"] == "say"
    assert pub["envVars"] == ["FOO"]
    assert pub["secretVars"] == ["TOK"]
    # argv template / exec path are not part of the server-facing schema
    assert "argv" not in pub
    assert "exec" not in pub


def test_launch_completion_is_explicit_and_public():
    tool = _echo_tool(completion="launch")
    assert tool.completion == "launch"
    assert tool.public()["completion"] == "launch"


def test_public_schema_marks_sensitive_params():
    tool = _echo_tool(
        params=[
            {"name": "msg", "type": "string", "required": True},
            {"name": "password", "type": "string", "required": True, "sensitive": True},
        ],
    )
    params = {p["name"]: p for p in tool.public()["params"]}
    assert params["password"].get("sensitive") is True
    assert "sensitive" not in params["msg"]  # only set when true


def test_malformed_entries_skipped_on_load(tmp_path):
    path = tmp_path / "tools.json"
    path.write_text(json.dumps({"tools": [{"bogus": True}, _echo_tool().model_dump(by_alias=True)]}),
                    encoding="utf-8")
    reg = ToolRegistry.load(path=path)
    assert [t.name for t in reg.list()] == ["say"]


def test_malformed_registry_document_is_rejected_without_overwrite(tmp_path):
    path = tmp_path / "tools.json"
    path.write_text("{", encoding="utf-8")

    with pytest.raises(LocalStateError, match="有效 JSON"):
        ToolRegistry.load(path=path)
    assert path.read_text(encoding="utf-8") == "{"
