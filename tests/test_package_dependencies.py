"""Cover automatic dependency recovery for package-local update hooks.

The hook callback is synthetic and installation is mocked at its external
boundary. Virtual-environment creation and package-index traffic are out of
scope.
"""

from __future__ import annotations

import sys
from types import ModuleType
from unittest import mock

from pkg.dependencies import run_with_missing_dependencies


def test_missing_hook_dependency_is_installed_before_the_hook_retries() -> None:
    """A package-local hook retries after its missing import is installed."""
    dependency = "pkg_test_dependency"
    sys.modules.pop(dependency, None)

    def hook() -> str:
        __import__(dependency)
        return "updated"

    def install(module_name: str) -> None:
        assert module_name == dependency
        sys.modules[module_name] = ModuleType(module_name)

    try:
        with mock.patch("pkg.dependencies.install_missing_dependency", side_effect=install):
            assert run_with_missing_dependencies(hook) == "updated"
    finally:
        sys.modules.pop(dependency, None)
