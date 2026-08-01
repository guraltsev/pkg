"""Cover opt-in dependency recovery for package-local update hooks.

The hook callback is synthetic and installation is mocked at its external
boundary. Virtual-environment creation and package-index traffic are out of
scope.
"""

from __future__ import annotations

import sys
from types import ModuleType
from unittest import mock

from pkg.dependencies import (
    MissingLocalDependencyError,
    ensure_dependency,
    run_with_missing_dependencies,
)


def test_missing_runtime_dependency_is_installed_automatically() -> None:
    """A pkg-owned feature provisions its missing dependency before continuing."""
    dependency = "pkg_test_runtime_dependency"
    sys.modules.pop(dependency, None)

    def install(module_name: str) -> None:
        assert module_name == dependency
        sys.modules[module_name] = ModuleType(module_name)

    try:
        with mock.patch("pkg.dependencies.install_missing_dependency", side_effect=install):
            ensure_dependency(dependency)
        assert dependency in sys.modules
    finally:
        sys.modules.pop(dependency, None)


def test_missing_hook_dependency_is_reported_without_installing() -> None:
    """A package-local hook reports an unavailable import by default."""
    dependency = "pkg_test_dependency"
    sys.modules.pop(dependency, None)

    def hook() -> str:
        __import__(dependency)
        return "updated"

    def install(module_name: str) -> None:
        assert module_name == dependency
        sys.modules[module_name] = ModuleType(module_name)

    with mock.patch("pkg.dependencies.install_missing_dependency", side_effect=install) as installer:
        try:
            run_with_missing_dependencies(hook)
        except MissingLocalDependencyError as error:
            assert "Package-local dependency unavailable" in str(error)
            assert "--local-deps-autoinstall" in str(error)
        else:
            raise AssertionError("Missing local dependencies must be reported by default")
    installer.assert_not_called()


def test_missing_hook_dependency_installs_after_explicit_opt_in() -> None:
    """An opted-in package-local hook retries after its import is installed."""
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
            assert run_with_missing_dependencies(hook, autoinstall=True) == "updated"
    finally:
        sys.modules.pop(dependency, None)
