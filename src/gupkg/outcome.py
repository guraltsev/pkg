"""Describe the result of one gupkg command and format it for people or scripts.

Every front end (command line, package TUI, manager TUI) receives the same
:class:`Outcome` from the operations in :mod:`gupkg.commands` and shows it with
the same formatters, so the wording, status names, and machine-readable layout
cannot differ between them.

Usage and API
-------------
``failure(...)`` builds a failed outcome. ``format_human(...)`` returns the text
a person should see (standard output and standard error parts), and
``format_toml(...)`` returns one parseable ``output_schema = 1`` document.

Implementation Approach
-----------------------
An outcome carries the workflow's :class:`~gupkg.core.ActionResult` plus
optional structured ``data`` sections (``package``, ``config``, ``manager``,
``registry``, ``self``, ``summary``) and record lists (``targets``,
``registry_packages``, ``self_shims``). Formatters walk those sections in a
fixed order, which keeps output deterministic.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .core import EXIT_USER_ERROR, ActionResult


@dataclass
class Outcome:
    """Hold one command result and its optional command-specific records."""

    command: str
    result: ActionResult
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> str:
        """Return the reported status, defaulting to changed, current, or failed."""
        result = self.result
        return result.status or (
            "failed" if not result.ok else ("changed" if result.changed else "current")
        )


def failure(command: str, message: str, code: int = EXIT_USER_ERROR) -> Outcome:
    """Create a failed outcome that has not touched package state."""
    return Outcome(command, ActionResult(ok=False, errors=[message], exit_code=code))


# Record sections rendered as TOML tables (and ``section.key`` human lines).
_TABLE_SECTIONS = ("package", "config", "manager", "registry", "self", "summary")
# Record lists rendered as TOML arrays of tables, with their human prefixes.
_ARRAY_SECTIONS = (
    ("targets", "target", "target"),
    ("registry_packages", "registry.package", "registry package"),
    ("self_shims", "self.shim", "self shim"),
)


def format_human(outcome: Outcome) -> tuple[str, str]:
    """Format an outcome for people.

    Parameters
    ----------
    outcome : Outcome
        Result to present.

    Returns
    -------
    tuple[str, str]
        ``(stdout_text, stderr_text)``: the status line and records, then the
        warnings and errors, each reported exactly once.
    """
    lines = [f"{outcome.command}: {outcome.status}"]
    package = outcome.data.get("package")
    if isinstance(package, dict) and package.get("identity"):
        active = package.get("installed_version")
        lines.append(
            f"  {package['identity']} {package['version']}"
            + (f" (active: {active})" if active else " (not active)")
            + f" [{package['scope']} scope]"
        )
    config = outcome.data.get("config")
    if isinstance(config, dict):
        if config.get("operation") != "check":
            lines.append(f"  {config['path']}")
        if config.get("backup_path"):
            lines.append(f"  previous version kept as {config['backup_path']}")
    for section in ("manager", "registry", "self"):
        values = outcome.data.get(section)
        if isinstance(values, dict):
            lines.extend(f"{section}.{key}: {value}" for key, value in values.items() if value is not None)
    for key, _, label in _ARRAY_SECTIONS:
        for record in outcome.data.get(key, []):
            details = ", ".join(
                f"{name}={value}" for name, value in record.items() if value not in (None, [], "")
            )
            lines.append(f"{label}: {details}")
    summary = outcome.data.get("summary")
    if isinstance(summary, dict):
        lines.append("summary: " + ", ".join(f"{key}={value}" for key, value in summary.items()))
    problems = [f"WARNING: {warning}" for warning in outcome.result.warnings]
    problems += [f"ERROR: {error}" for error in outcome.result.errors]
    return "\n".join(lines) + "\n", "".join(line + "\n" for line in problems)


def format_toml(outcome: Outcome) -> str:
    """Format an outcome as one deterministic, versioned TOML document."""
    result = outcome.result
    lines = [
        "output_schema = 1",
        f"command = {_toml_value(outcome.command)}",
        f"ok = {_toml_value(result.ok)}",
        f"changed = {_toml_value(result.changed)}",
        f"status = {_toml_value(outcome.status)}",
        f"exit_code = {result.exit_code}",
        f"warnings = {_toml_value(result.warnings)}",
        f"errors = {_toml_value(result.errors)}",
    ]

    def table(header: str, values: dict[str, Any]) -> None:
        """Append one table, omitting unset values."""
        lines.append(f"\n{header}")
        lines.extend(f"{key} = {_toml_value(value)}" for key, value in values.items() if value is not None)

    for section in _TABLE_SECTIONS:
        if isinstance(outcome.data.get(section), dict):
            table(f"[{section}]", outcome.data[section])
    for key, header, _ in _ARRAY_SECTIONS:
        for record in outcome.data.get(key, []):
            table(f"[[{header}]]", record)
    return "\n".join(lines) + "\n"


def _toml_value(value: Any) -> str:
    """Render one scalar or list as TOML; other values become strings."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(str(item)) for item in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)
