"""Declarative local tool registry for controlled execution (add-connector-local-tools).

The device owner registers tools in ``~/.nanobot-connector/tools.json``. The server
can only invoke a tool *by name* with structured arguments — it can never construct
an arbitrary command line. Every argument is validated against the tool's parameter
templates and rendered into an ``argv`` list that runs WITHOUT a shell, so shell
metacharacters inside a value can never be interpreted.

Credentials never leave this machine: literal env vars live in the tool definition,
while secret references map an env var to an id in the operating system credential
store. ``tools.list`` exposes only names/params/approval and the *names* of required
env/secret vars — never any secret value.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

from nanobot_connector.config import config_dir
from nanobot_connector.credentials import CredentialStoreError, SecretStore
from nanobot_connector.persistence import (
    LocalStateConflictError,
    LocalStateError,
    file_fingerprint,
    locked_file,
    write_json_atomic,
)

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")
_NAME_RE = re.compile(r"^[a-zA-Z0-9_.-]+$")

ApprovalPolicy = Literal["auto", "webui", "local"]
CompletionPolicy = Literal["wait", "launch"]
ParamType = Literal["string", "int", "enum", "path"]


class ToolError(Exception):
    """Base for tool-registry failures, carrying a stable protocol ``code``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ToolNotFoundError(ToolError):
    def __init__(self, name: str) -> None:
        super().__init__("tool_not_found", f"tool not registered: {name}")


class InvalidArgsError(ToolError):
    def __init__(self, message: str) -> None:
        super().__init__("invalid_args", message)


class MissingCredentialError(ToolError):
    def __init__(self, message: str) -> None:
        super().__init__("missing_credential", message)


class _Model(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class ToolParam(_Model):
    """One declared parameter of a tool.

    ``path`` params MUST declare ``allowed_dir``: the supplied value is resolved
    (``..``/symlinks collapsed) and required to stay inside it, so the server cannot
    steer a tool at arbitrary files.
    """

    name: str
    type: ParamType = "string"
    required: bool = False
    description: str = ""
    choices: list[str] = Field(default_factory=list)  # type == enum
    pattern: str = ""  # type == string, full-match regex
    min: int | None = None  # type == int
    max: int | None = None  # type == int
    allowed_dir: str = ""  # type == path
    sensitive: bool = False  # redact value in audit/approval summaries

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError(f"invalid param name: {v!r}")
        return v


class ToolDef(_Model):
    """A single registered tool. ``argv`` is a fixed token template.

    Each token is either a literal or contains ``{param}`` placeholders. A token
    whose referenced param is omitted (and optional) is dropped; otherwise the value
    is substituted and kept as a single argv element (never shell-split).
    """

    name: str
    description: str = ""
    exec: str
    params: list[ToolParam] = Field(default_factory=list)
    argv: list[str] = Field(default_factory=list)
    workdir: str = ""
    timeout_s: int | None = None  # falls back to the connector-wide exec timeout
    approval: ApprovalPolicy = "local"  # safe default: owner confirms on this machine
    completion: CompletionPolicy = "wait"  # launch returns after starting a GUI/app
    env: dict[str, str] = Field(default_factory=dict)  # literal, non-secret env vars
    secrets: dict[str, str] = Field(default_factory=dict)  # env var -> secret id

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError(f"invalid tool name: {v!r}")
        return v

    def public(self) -> dict[str, Any]:
        """Schema for ``tools.list`` — never leaks secret values, only var names."""
        return {
            "name": self.name,
            "description": self.description,
            "approval": self.approval,
            "completion": self.completion,
            "params": [
                {
                    "name": p.name,
                    "type": p.type,
                    "required": p.required,
                    "description": p.description,
                    **({"choices": p.choices} if p.choices else {}),
                    **({"sensitive": True} if p.sensitive else {}),
                }
                for p in self.params
            ],
            "envVars": sorted(self.env.keys()),
            "secretVars": sorted(self.secrets.keys()),
        }


class ToolRegistry:
    """Loads/saves ``tools.json`` and validates+renders invocations."""

    def __init__(self, tools: list[ToolDef] | None = None, *, path: Path | None = None,
                 secrets: SecretStore | None = None) -> None:
        self._path = path or (config_dir() / "tools.json")
        self._secrets = secrets or SecretStore()
        self._tools: dict[str, ToolDef] = {}
        self._snapshot = file_fingerprint(self._path)
        for t in tools or []:
            self._tools[t.name] = t

    # -- persistence --------------------------------------------------------

    @classmethod
    def load(cls, *, path: Path | None = None, secrets: SecretStore | None = None) -> "ToolRegistry":
        reg = cls(path=path, secrets=secrets)
        try:
            data = json.loads(reg._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return reg
        except (OSError, ValueError) as exc:
            raise LocalStateError(f"工具注册表不是有效 JSON：{reg._path}") from exc
        if not isinstance(data, dict):
            raise LocalStateError(f"工具注册表根节点必须是对象：{reg._path}")
        for row in data.get("tools", []) if isinstance(data, dict) else []:
            try:
                tool = ToolDef.model_validate(row)
            except Exception:  # noqa: BLE001 - skip malformed entries, keep the rest
                continue
            reg._tools[tool.name] = tool
        reg._snapshot = file_fingerprint(reg._path)
        return reg

    def save(self) -> None:
        payload = {"tools": [t.model_dump(by_alias=True, exclude_defaults=True) for t in self._tools.values()]}
        with locked_file(self._path):
            current = file_fingerprint(self._path)
            if current != self._snapshot:
                raise LocalStateConflictError(
                    "工具注册表已被其他连接器进程修改；请重新读取后再提交修改"
                )
            write_json_atomic(self._path, payload)
            self._snapshot = file_fingerprint(self._path)

    # -- registry ops -------------------------------------------------------

    def add(self, tool: ToolDef) -> None:
        self._tools[tool.name] = tool

    def remove(self, name: str) -> bool:
        return self._tools.pop(name, None) is not None

    def get(self, name: str) -> ToolDef:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolNotFoundError(name)
        return tool

    def list(self) -> list[ToolDef]:
        return list(self._tools.values())

    def list_public(self) -> list[dict[str, Any]]:
        return [t.public() for t in self._tools.values()]

    # -- validation + rendering --------------------------------------------

    def render(self, name: str, args: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
        """Validate *args* against the tool and return ``(argv, env_overlay)``.

        Raises :class:`ToolError` subclasses (mapped to protocol error codes) on any
        undeclared arg, failed constraint, or missing credential. Never runs a shell.
        """
        tool = self.get(name)
        args = args or {}
        declared = {p.name: p for p in tool.params}

        unknown = set(args) - set(declared)
        if unknown:
            raise InvalidArgsError(f"unknown argument(s): {', '.join(sorted(unknown))}")

        rendered: dict[str, str] = {}
        provided: set[str] = set()
        for param in tool.params:
            if param.name not in args or args[param.name] is None:
                if param.required:
                    raise InvalidArgsError(f"missing required argument: {param.name}")
                continue
            rendered[param.name] = _validate_param(param, args[param.name])
            provided.add(param.name)

        argv = [tool.exec] + _render_argv(tool.argv, rendered, provided)
        env = self._resolve_env(tool)
        return argv, env

    def _resolve_env(self, tool: ToolDef) -> dict[str, str]:
        env = dict(tool.env)
        for var, secret_id in tool.secrets.items():
            try:
                value = self._secrets.get(secret_id)
            except CredentialStoreError as exc:
                raise MissingCredentialError(str(exc)) from exc
            if value is None:
                raise MissingCredentialError(
                    f"credential '{secret_id}' for env var '{var}' is not configured on this device"
                )
            env[var] = value
        return env


def _validate_param(param: ToolParam, value: Any) -> str:
    if param.type == "enum":
        s = str(value)
        if s not in param.choices:
            raise InvalidArgsError(f"{param.name}: {s!r} not in {param.choices}")
        return s
    if param.type == "int":
        try:
            n = int(value)
        except (TypeError, ValueError) as exc:
            raise InvalidArgsError(f"{param.name}: expected integer") from exc
        if param.min is not None and n < param.min:
            raise InvalidArgsError(f"{param.name}: {n} < min {param.min}")
        if param.max is not None and n > param.max:
            raise InvalidArgsError(f"{param.name}: {n} > max {param.max}")
        return str(n)
    if param.type == "path":
        if not param.allowed_dir:
            raise InvalidArgsError(f"{param.name}: path param has no allowedDir configured")
        base = Path(param.allowed_dir).expanduser().resolve()
        resolved = Path(str(value)).expanduser().resolve()
        if resolved != base and base not in resolved.parents:
            raise InvalidArgsError(f"{param.name}: path escapes allowed directory")
        return str(resolved)
    # string
    s = str(value)
    if param.pattern and not re.fullmatch(param.pattern, s):
        raise InvalidArgsError(f"{param.name}: value does not match required pattern")
    return s


def _render_argv(template: list[str], values: dict[str, str], provided: set[str]) -> list[str]:
    """Substitute ``{param}`` placeholders. A token referencing an omitted optional
    param is dropped whole; every other token becomes exactly one argv element."""
    out: list[str] = []
    for token in template:
        refs = _PLACEHOLDER_RE.findall(token)
        if not refs:
            out.append(token)
            continue
        if any(ref not in provided for ref in refs):
            continue  # optional param omitted → drop this token
        out.append(_PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], token))
    return out
