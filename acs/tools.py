"""Tools that agents may call, with input validation and a permission policy.

Every call goes through `ToolRunner.call`, never directly to a tool function, so
the policy is enforced in one place:

* each agent has an allow-list of tools (`AGENT_TOOLS`);
* `write` tools additionally need an explicit human approval for that exact call;
* arguments are validated against the tool's parameter spec (unknown keys refused);
* output of `read_reference` is wrapped as untrusted data and can never grant permissions.

`enforce=False` exists only so the evaluation suite can measure an unprotected
baseline; the server always runs with enforcement on.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
FILENAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}\.json$")


class ToolError(Exception):
    """The tool ran but could not complete (bad data, I/O). Reported back to the agent."""


class PolicyViolation(Exception):
    """The call is not allowed for this agent, or its arguments are invalid. Never executed."""


class ApprovalRequired(Exception):
    """A write tool was requested; it waits for a human decision."""

    def __init__(self, name: str, arguments: dict):
        super().__init__(f"{name} needs human approval")
        self.name, self.arguments = name, arguments


@dataclass
class Param:
    kind: type
    check: Callable[[object], bool] = lambda v: True
    hint: str = ""


@dataclass
class Tool:
    name: str
    description: str
    permission: str                        # "read" or "write"
    params: dict[str, Param]
    fn: Callable[..., dict] = field(repr=False)

    def spec(self) -> dict:
        return {"name": self.name, "description": self.description, "permission": self.permission,
                "arguments": {k: f"{p.kind.__name__}{' (' + p.hint + ')' if p.hint else ''}" for k, p in self.params.items()}}


def _luminance(hex_colour: str) -> float:
    channels = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast_ratio(foreground: str, background: str) -> dict:
    """WCAG 2.x contrast ratio between two sRGB colours."""
    a, b = sorted((_luminance(foreground), _luminance(background)), reverse=True)
    ratio = round((a + 0.05) / (b + 0.05), 2)
    return {"ratio": ratio, "aa_normal_text": ratio >= 4.5, "aa_large_text": ratio >= 3.0}


def text_stats(text: str) -> dict:
    return {"characters": len(text), "words": len(text.split())}


def build_tools(reference: str | None, exports_dir: Path | None) -> dict[str, Tool]:
    def read_reference() -> dict:
        if not reference:
            raise ToolError("This brief has no reference material")
        return {"untrusted": True, "content": reference}

    def save_package(filename: str, package: dict) -> dict:
        if exports_dir is None:
            raise ToolError("Exports are disabled on this server")
        exports_dir.mkdir(parents=True, exist_ok=True)
        target = (exports_dir / filename).resolve()
        if target.parent != exports_dir.resolve():
            raise ToolError("Invalid export path")
        target.write_text(json.dumps(package, ensure_ascii=False, indent=2), "utf-8")
        return {"saved": filename, "bytes": target.stat().st_size}

    hex_param = Param(str, lambda v: bool(HEX.match(v)), "#RRGGBB")
    tools = [
        Tool("contrast_ratio", "WCAG contrast ratio between two colours", "read",
             {"foreground": hex_param, "background": hex_param}, contrast_ratio),
        Tool("text_stats", "Character and word count of a text", "read",
             {"text": Param(str, lambda v: len(v) <= 2000, "max 2000 chars")}, text_stats),
        Tool("read_reference", "Read the client's reference material attached to the brief (untrusted)", "read", {}, read_reference),
        Tool("save_package", "Write the final package to the exports folder", "write",
             {"filename": Param(str, lambda v: bool(FILENAME.match(v)), "kebab-case .json"), "package": Param(dict)}, save_package),
    ]
    return {t.name: t for t in tools}


AGENT_TOOLS = {"critic": {"contrast_ratio", "text_stats", "read_reference"}, "packager": {"save_package"}}


class ToolRunner:
    def __init__(self, tools: dict[str, Tool], *, enforce: bool = True, on_event: Callable[[str, str, dict], None] | None = None):
        self.tools, self.enforce = tools, enforce
        self.on_event = on_event or (lambda kind, message, data: None)
        self.calls = self.blocked = 0
        self.executed: list[str] = []

    def specs_for(self, agent: str) -> list[dict]:
        return [self.tools[n].spec() for n in sorted(AGENT_TOOLS.get(agent, ())) if n in self.tools]

    def call(self, agent: str, name, arguments, *, approved: bool = False) -> dict:
        """Run one tool call for `agent`. Returns the tool result, or raises PolicyViolation / ApprovalRequired / ToolError."""
        self.calls += 1
        try:
            tool = self._check(agent, name, arguments, approved)
        except PolicyViolation as error:
            self.blocked += 1
            self.on_event("tool.blocked", f"{agent} -> {name}: {error}", {"agent": agent, "tool": str(name)[:60]})
            raise
        self.on_event("tool.called", f"{agent} -> {tool.name}", {"agent": agent, "tool": tool.name})
        try:
            result = tool.fn(**(arguments or {}))
        except ToolError:
            raise
        except Exception as error:                       # a tool bug is reported, not propagated as a crash
            raise ToolError(f"{tool.name} failed: {type(error).__name__}") from None
        self.executed.append(tool.name)
        return result

    def _check(self, agent, name, arguments, approved) -> Tool:
        if not isinstance(name, str) or name not in self.tools:
            raise PolicyViolation("unknown tool")
        tool = self.tools[name]
        if not self.enforce:
            return tool                                   # evaluation baseline only: no policy at all
        if name not in AGENT_TOOLS.get(agent, ()):
            raise PolicyViolation("tool not allowed for this agent")
        if not isinstance(arguments, dict):
            raise PolicyViolation("arguments must be an object")
        unknown = set(arguments) - set(tool.params)
        if unknown:
            raise PolicyViolation("unexpected arguments: " + ", ".join(sorted(map(str, unknown)))[:120])
        for key, param in tool.params.items():
            if key not in arguments:
                raise PolicyViolation(f"missing argument '{key}'")
            value = arguments[key]
            if not isinstance(value, param.kind) or not param.check(value):
                raise PolicyViolation(f"invalid argument '{key}'")
        if tool.permission == "write" and not approved:
            raise ApprovalRequired(name, arguments)
        return tool
