from __future__ import annotations

import ast
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
PKG_PY = ROOT / "pkg.py"
DOC_TARGETS = [
    PKG_PY,
]
README = ROOT / "README.md"
DOC_INDEX = ROOT / "docs" / "README.md"
WRAPPER_SCRIPTS = [
    ROOT / "install.cmd",
    ROOT / "install-machine.cmd",
    ROOT / "update-config.cmd",
]
REMOVED_MODULES = [
    ROOT / "pkg_common.py",
    ROOT / "pkg_core.py",
    ROOT / "pkg_windows.py",
]
SECTION_MARKERS = {
    "shared": "# Section: Shared models and pure helpers",
    "windows": "# Section: Windows integration boundary",
    "core": "# Section: Package-management logic and CLI",
    "entry": "# Section: Script entry point",
}


class DefinitionDocVisitor(ast.NodeVisitor):
    """Collect function and class definitions from a parsed syntax tree."""

    def __init__(self) -> None:
        """Initialise storage for discovered definitions and their qualnames."""
        self.stack: list[str] = []
        self.items: list[tuple[str, ast.AST]] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """Record a class definition and recursively inspect its contents."""
        self.stack.append(node.name)
        self.items.append((".".join(self.stack), node))
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """Record a synchronous function definition and inspect nested items."""
        self.stack.append(node.name)
        self.items.append((".".join(self.stack), node))
        self.generic_visit(node)
        self.stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """Record an asynchronous function definition and inspect nested items."""
        self.stack.append(node.name)
        self.items.append((".".join(self.stack), node))
        self.generic_visit(node)
        self.stack.pop()


class DocumentationCoverageTests(unittest.TestCase):
    def _definition_items(self) -> list[tuple[Path, str, ast.AST]]:
        """Return all documented class and function definitions in the target files."""
        items: list[tuple[Path, str, ast.AST]] = []
        for path in DOC_TARGETS:
            visitor = DefinitionDocVisitor()
            visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
            items.extend((path, qualname, node) for qualname, node in visitor.items)
        return items

    def test_every_function_and_class_has_a_docstring(self) -> None:
        """Ensure all classes and functions carry an explicit docstring."""
        missing: list[str] = []
        for path, qualname, node in self._definition_items():
            if ast.get_docstring(node) is None:
                missing.append(f"{path.name}:{node.lineno}:{qualname}")
        self.assertEqual(missing, [], msg="Missing docstrings:\n" + "\n".join(missing))

    def test_every_function_docstring_mentions_its_parameters(self) -> None:
        """Require function docstrings to mention each documented parameter by name."""
        missing_mentions: list[str] = []
        for path, qualname, node in self._definition_items():
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            docstring = ast.get_docstring(node) or ""
            docstring_lower = docstring.lower()
            parameters = [
                arg.arg
                for arg in (
                    node.args.posonlyargs + node.args.args + node.args.kwonlyargs
                )
            ]
            if node.args.vararg is not None:
                parameters.append(node.args.vararg.arg)
            if node.args.kwarg is not None:
                parameters.append(node.args.kwarg.arg)
            for parameter in parameters:
                if parameter in {"self", "cls"}:
                    continue
                pattern = rf"\b{re.escape(parameter.lower())}\b"
                if re.search(pattern, docstring_lower) is None:
                    missing_mentions.append(f"{path.name}:{node.lineno}:{qualname}:{parameter}")
        self.assertEqual(
            missing_mentions,
            [],
            msg="Docstrings missing parameter coverage:\n" + "\n".join(missing_mentions),
        )

    def test_readme_exposes_documentation_index_and_core_docs(self) -> None:
        """Verify the top-level README links readers to the main documentation pages."""
        readme_text = README.read_text(encoding="utf-8")
        self.assertIn("docs/README.md", readme_text)
        self.assertIn("docs/architecture.md", readme_text)
        self.assertIn("docs/configuration.md", readme_text)
        self.assertIn("docs/api.md", readme_text)
        self.assertIn("docs/review.md", readme_text)

    def test_readme_describes_single_file_architecture(self) -> None:
        """Verify the README explains that implementation lives in ``pkg.py``."""
        readme_text = README.read_text(encoding="utf-8")
        self.assertIn("entirely in `pkg.py`", readme_text)
        self.assertIn("Windows integration boundary", readme_text)
        self.assertIn("Package-management logic and CLI", readme_text)

    def test_docs_index_mentions_all_discoverable_docs(self) -> None:
        """Verify the documentation index links every intentionally discoverable guide."""
        index_text = DOC_INDEX.read_text(encoding="utf-8")
        self.assertIn("../README.md", index_text)
        self.assertIn("architecture.md", index_text)
        self.assertIn("configuration.md", index_text)
        self.assertIn("api.md", index_text)
        self.assertIn("review.md", index_text)


class WrapperScriptTests(unittest.TestCase):
    def test_wrapper_scripts_preserve_caller_working_directory(self) -> None:
        """Ensure convenience wrappers do not change away from the caller's directory."""
        for script in WRAPPER_SCRIPTS:
            script_text = script.read_text(encoding="utf-8")
            self.assertNotIn("cd /d", script_text.lower(), msg=f"Wrapper unexpectedly changes directory: {script.name}")


class ArchitectureBoundaryTests(unittest.TestCase):
    def _source(self) -> str:
        """Return the full source text of ``pkg.py``."""
        return PKG_PY.read_text(encoding="utf-8")

    def _module_tree(self) -> ast.Module:
        """Return the parsed syntax tree of ``pkg.py``."""
        return ast.parse(self._source())

    def _section_ranges(self) -> dict[str, tuple[int, int]]:
        """Return byte ranges for each named section in ``pkg.py``."""
        source = self._source()
        positions = {name: source.index(marker) for name, marker in SECTION_MARKERS.items()}
        ordered = sorted(positions.items(), key=lambda item: item[1])
        ranges: dict[str, tuple[int, int]] = {}
        for index, (name, start) in enumerate(ordered):
            end = ordered[index + 1][1] if index + 1 < len(ordered) else len(source)
            ranges[name] = (start, end)
        return ranges

    def test_single_file_layout_removes_old_implementation_modules(self) -> None:
        """Ensure the old split implementation files are no longer present."""
        for path in REMOVED_MODULES:
            self.assertFalse(path.exists(), msg=f"Unexpected legacy module present: {path.name}")

    def test_legacy_conversion_script_has_been_removed(self) -> None:
        """Ensure the old JSON-to-TOML migration helper is gone from the repo."""
        self.assertFalse((ROOT / "helper_scripts" / "legacy_to_pkg_toml.py").exists())

    def test_pkg_py_contains_required_section_markers(self) -> None:
        """Ensure ``pkg.py`` exposes the expected architecture sections."""
        source = self._source()
        for marker in SECTION_MARKERS.values():
            self.assertIn(marker, source)

    def test_pkg_metadata_does_not_restore_removed_private_compatibility_methods(self) -> None:
        """Ensure removed private compatibility wrappers stay out of ``PackageMetadata``."""
        tree = self._module_tree()
        class_node = next(
            node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "PackageMetadata"
        )
        method_names = {
            node.name for node in class_node.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for removed_name in {
            "_fill_from_directory",
            "_fill_current",
            "_validate_config_dict",
            "_to_toml_scalar",
            "_metadata_sync_payload",
            "_create_starter_config_text",
            "_locate_metadata_container",
            "_load_from_dict",
        }:
            self.assertNotIn(removed_name, method_names)

    def test_install_context_wrapper_class_does_not_return(self) -> None:
        """Ensure install steps keep the direct reporter-based signature."""
        tree = self._module_tree()
        class_names = {node.name for node in tree.body if isinstance(node, ast.ClassDef)}
        self.assertNotIn("InstallContext", class_names)

    def test_removed_compatibility_and_facade_symbols_stay_removed(self) -> None:
        """Ensure deleted compatibility surfaces do not reappear in ``pkg.py``."""
        source = self._source()
        for marker in (
            "class WindowsPlatform",
            "class VariableExpander",
            "def package_config_to_dict(",
            "def create_shortcut_with_pywin32(",
            "def create_shortcut_with_powershell(",
            "def load_roundtrip_toml_backend(",
            "def load_config_document(",
            "--python",
            "--no-autoupdate-config",
        ):
            self.assertNotIn(marker, source)

    def test_shared_windows_and_core_sections_begin_with_imports(self) -> None:
        """Ensure each implementation section begins with imports after its header."""
        lines = PKG_PY.read_text(encoding="utf-8").splitlines()
        for marker in (
            SECTION_MARKERS["shared"],
            SECTION_MARKERS["windows"],
            SECTION_MARKERS["core"],
        ):
            marker_index = lines.index(marker)
            candidate = ""
            for line in lines[marker_index + 1 :]:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                candidate = stripped
                break
            self.assertTrue(
                candidate.startswith("import ") or candidate.startswith("from "),
                msg=f"Section does not begin with an import block after header: {marker}",
            )

    def test_windows_markers_are_confined_to_windows_section(self) -> None:
        """Ensure direct Windows primitives appear only inside the Windows section."""
        source = self._source()
        ranges = self._section_ranges()
        windows_start, windows_end = ranges["windows"]
        banned_markers = [
            "WScript.Shell",
            "SendMessageTimeoutW",
            "HKEY_CURRENT_USER",
            "HKEY_LOCAL_MACHINE",
            "import subprocess",
            "import ctypes",
            "import winreg",
        ]
        for marker in banned_markers:
            for match in re.finditer(re.escape(marker), source):
                self.assertGreaterEqual(match.start(), windows_start, msg=f"Marker outside Windows section: {marker}")
                self.assertLess(match.start(), windows_end, msg=f"Marker outside Windows section: {marker}")

    def test_core_section_contains_no_direct_windows_mutation_markers(self) -> None:
        """Ensure the package-management section stays free of Windows primitives."""
        source = self._source()
        start, end = self._section_ranges()["core"]
        core_source = source[start:end]
        banned_markers = [
            "WScript.Shell",
            "SendMessageTimeoutW",
            "HKEY_CURRENT_USER",
            "HKEY_LOCAL_MACHINE",
            "import subprocess",
            "import ctypes",
            "import winreg",
        ]
        for marker in banned_markers:
            self.assertNotIn(marker, core_source)

    def test_windows_section_keeps_only_wrapper_functions_for_shortcuts_and_registry(self) -> None:
        """Ensure the Windows section holds wrapper functions while orchestration classes stay out."""
        source = self._source()
        start, end = self._section_ranges()["windows"]
        windows_source = source[start:end]
        self.assertIn("def create_shortcut(", windows_source)
        self.assertIn("def read_registry_value(", windows_source)
        self.assertIn("def write_registry_value(", windows_source)
        self.assertNotIn("class ShortcutInstaller", windows_source)
        self.assertNotIn("class EnvironmentVariableManager", windows_source)
        self.assertNotIn("class PATHManager", windows_source)
        self.assertNotIn("class BinFileCreator", windows_source)
        self.assertNotIn("class WindowsPlatform", windows_source)
        self.assertNotIn("class JunctionManager", windows_source)

    def test_orchestration_classes_live_in_core_section(self) -> None:
        """Ensure install orchestration classes are defined in the package-management section."""
        source = self._source()
        start, end = self._section_ranges()["core"]
        core_source = source[start:end]
        for marker in (
            "class JunctionManager",
            "class ShortcutInstaller",
            "class EnvironmentVariableManager",
            "class PATHManager",
            "class BinFileCreator",
        ):
            self.assertIn(marker, core_source)
        for marker in (
            "def resolve_input_path(",
            "def compute_scope_paths(",
        ):
            self.assertIn(marker, core_source)


if __name__ == "__main__":
    unittest.main()
