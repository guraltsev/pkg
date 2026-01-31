#!/usr/bin/env python3
"""
Local Package Manager for Windows (gu-opt-pkg)
A tool to install software packages from a standardized directory structure.
Creates junctions, shortcuts, environment variables, PATH entries, and executable wrappers.

Version: 0.10.0
Copyright (C) 2025 Gennady Uraltseev. All rights reserved.

Licensed under the MIT License.

"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import stat
from enum import Enum
from pathlib import Path, PureWindowsPath
from typing import Dict, List, Optional, Tuple, Union
import winreg


def normalize_path(path: str|Path) -> str:
    path_str = str(path)
    if path_str.startswith('\\\\?\\'):
            path_str = path_str[4:]
    return Path(path_str).resolve()

# Try to import pywin32 for shortcut creation
try:
    import win32com.client
    PYWIN32_AVAILABLE = True
except ImportError:
    PYWIN32_AVAILABLE = False

# Try to import TOML
try:
    import toml
    TOML_AVAILABLE = True
except ImportError:
    TOML_AVAILABLE = False

class Scope(Enum):
    """Installation scope: User (per-user) or Machine (system-wide)."""
    USER = "User"
    MACHINE = "Machine"

class Action(Enum):
    """Available actions for the package manager."""
    INSTALL = "Install"
    UPDATE_CONFIG = "UpdateConfig"
    CONVERT_JSON_TO_TOML = "ConvertJSONToTOML"
    COMPRESS = "Compress"

class PackageMetadata:
    """Manages package information from directory structure and configuration."""
    
    def __init__(self, version_path: Path):
        """
        Initialize package metadata.
        
        Args:
            version_path: Path to the package version directory (e.g., PackageName\v1.0.0.l1)
        """
        self.version_path = Path(version_path)
        self.pkg_path = self.version_path.parent
        self.name = self.pkg_path.name
        self.version = ""
        self.local_version = ""
        self.version_string = ""
        self.is_current = False
        self.scope = Scope.USER
        self.component_paths: Dict[str, Path] = {}
        self.shortcut_dir = ""
        self.only_portable = False
        
        # Fields from configuration
        self.description: Optional[str] = None
        self.homepage: Optional[str] = None
        self.download_url: Optional[str] = None
        self.environment: List[Dict[str, str]] = []
        self.bin: List[Dict[str, str]] = []
        self.path: List[str] = []
        self.shortcut: List[Dict[str, str]] = []
        
        self._fill_from_directory()
        self._fill_current()
        
    def _fill_from_directory(self) -> None:
        """Extract package metadata from directory structure."""
        # Parse version directory name (e.g., "v1.2.3.l4", "v1.2-beta.3.l4")
        # We use relative_to to get the version directory name relative to the package directory
        # This is necessary because version_path could be ".", which would result in an empty string
        # Using relative_to ensures we get the version information even if version_path is "."
        version_dir_name = str(self.version_path.relative_to(self.pkg_path))
        
        # Match pattern vXXXXX.lNNNNN where XXXXX is the upstream version
        pattern = r'^v(.+)\.l(\d+)$'
        match = re.match(pattern, version_dir_name)
        
        if not match:
            raise ValueError(f"Invalid version directory name: {version_dir_name}. "
                           f"Expected format: vX.Y.Z.lN")
        
        self.version = match.group(1)  # Upstream version
        self.local_version = match.group(2)  # Local revision
        self.version_string = version_dir_name
        
        # Check if package name ends with -portable
        if self.name.lower().endswith('-portable'):
            self.only_portable = True
        
        # Set component paths
        self.component_paths = {
            "App": self.version_path / "App",
            "Icons": self.version_path / "Icons",
            "Shortcuts": self.version_path / "Shortcuts"
        }
    
    def _fill_current(self) -> None:
        """Check if current junction exists and what it points to."""
        current_path = self.pkg_path / "current"
        
        if current_path.exists():
            try:
                # Check if it's a junction by trying to read its target
                target = os.readlink(str(current_path)) if hasattr(os, 'readlink') else None
                if target:
                    self.is_current = os.path.normpath(str(self.version_path)) == os.path.normpath(target)
            except (OSError, AttributeError):
                # Not a junction or can't read it
                pass
    
    def check_metadata_consistency(self, config_data: Dict) -> List[str]:
        """
        Check consistency between directory-derived metadata and config file metadata.
        
        Args:
            config_data: Configuration data loaded from file
            
        Returns:
            List of inconsistency messages, empty if consistent
        """
        inconsistencies = []
        
        # Check name consistency
        config_name = config_data.get('name', '')
        if config_name and config_name != self.name:
            inconsistencies.append(f"Name mismatch: directory='{self.name}', config='{config_name}'")
        
        # Check version consistency
        config_version = config_data.get('version', '')
        if config_version and config_version != self.version:
            inconsistencies.append(f"Version mismatch: directory='{self.version}', config='{config_version}'")
        
        # Check localVersion consistency
        config_local_version = config_data.get('localVersion', '')
        if config_local_version and str(config_local_version) != self.local_version:
            inconsistencies.append(f"LocalVersion mismatch: directory='{self.local_version}', config='{config_local_version}'")
        
        # Check portable flag consistency
        config_only_portable = config_data.get('only_portable', None)
        if config_only_portable is not None and config_only_portable != self.only_portable:
            inconsistencies.append(f"Portable flag mismatch: directory='{self.only_portable}', config='{config_only_portable}'")
        
        return inconsistencies

    def load_config(self) -> Dict:
        """Load configuration from TOML or JSON. Returns the loaded data."""
        # Default empty data
        default_data = {
            "name": self.name,
            "version": self.version,
            "localVersion": self.local_version,
            "description": None,
            "homepage": None,
            "downloadURL": None,
            "environment": [],
            "bin": [],
            "path": [],
            "shortcut": [],
            "only_portable": self.only_portable
        }
        
        # Try TOML first
        toml_path = self.version_path / "opt_pkg.toml"
        json_path = self.version_path / "opt_pkg.json"
        
        if toml_path.exists():
            if not TOML_AVAILABLE:
                print("Warning: TOML file found but 'toml' package is not installed.")
                print("Install it with: pip install toml")
                if json_path.exists():
                    print("Falling back to JSON configuration.")
                else:
                    print("No JSON configuration found. Using defaults.")
                    self._load_from_dict(default_data)
                    return default_data
            
            try:
                with open(toml_path, 'r', encoding='utf-8') as f:
                    data = toml.load(f)
                self._load_from_dict(data)
                return data
            except Exception as e:
                print(f"Warning: Error loading TOML config: {e}")
                if json_path.exists():
                    print("Falling back to JSON configuration.")
                else:
                    print("Using defaults.")
                    self._load_from_dict(default_data)
                    return default_data
        
        # Fall back to JSON
        if json_path.exists():
            try:
                with open(json_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self._load_from_dict(data)
                return data
            except Exception as e:
                raise RuntimeError(f"Error loading JSON config from {json_path}: {e}") from e
    
    
    def _load_from_dict(self, data: Dict) -> None:
        """Load configuration from dictionary."""
        self.description = data.get('description', self.description)
        self.homepage = data.get('homepage', self.homepage)
        self.download_url = data.get('downloadURL', self.download_url)
        self.environment = data.get('environment', self.environment)
        self.bin = data.get('bin', self.bin)
        self.path = data.get('path', [])
        self.shortcut = data.get('shortcut', self.shortcut)
        
        # Check for portable flag
        if 'portable' in data:
            self.only_portable = bool(data['portable'])
        elif self.name.lower().endswith('-portable'):
            self.only_portable = True
    
    def update_config(self, data: Optional[Dict] = None) -> None:
        """Write metadata back to configuration file (prefer TOML)."""
        if data is None:
            data = {
                "name": self.name,
                "version": self.version,
                "localVersion": self.local_version,
                "description": self.description,
                "homepage": self.homepage,
                "downloadURL": self.download_url,
                "environment": self.environment,
                "bin": self.bin,
                "path": self.path,
                "shortcut": self.shortcut,
                "only_portable": self.only_portable
            }
        else:
            # Overwrite all fields from current metadata
            data["name"] = self.name
            data["version"] = self.version
            data["localVersion"] = self.local_version
            data["only_portable"] = self.only_portable
        
        # Always update portable flag if package name indicates it
        if self.name.lower().endswith('-portable') and not self.only_portable:
            self.only_portable = True
            data['only_portable'] = True
        
        # Prefer TOML if available
        if TOML_AVAILABLE:
            toml_path = self.version_path / "opt_pkg.toml"
            json_path = self.version_path / "opt_pkg.json"
            
            # Write TOML
            with open(toml_path, 'w', encoding='utf-8') as f:
                toml.dump(data, f)
            
            print(f"Updated: {toml_path}")
            
            # Remove JSON if it exists (convert to TOML)
            if json_path.exists():
                json_path.unlink()
                print(f"Removed: {json_path} (converted to TOML)")
        else:
            # Fall back to JSON
            json_path = self.version_path / "opt_pkg.json"
            with open(json_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            
            print(f"Updated: {json_path}")
    
    def _create_default_config(self) -> None:
        """Create a default configuration file."""
        default_data = {
            "name": self.name,
            "version": self.version,
            "localVersion": self.local_version,
            "description": None,
            "homepage": None,
            "downloadURL": None,
            "environment": [],
            "bin": [],
            "path": [],
            "shortcut": [],
            "only_portable": self.only_portable
        }
        
        self.update_config(default_data)
    
    def convert_json_to_toml(self) -> bool:
        """Convert existing JSON configuration to TOML."""
        if not TOML_AVAILABLE:
            print("Error: 'toml' package is required for TOML conversion.")
            print("Install it with: pip install toml")
            return False
        
        json_path = self.version_path / "opt_pkg.json"
        toml_path = self.version_path / "opt_pkg.toml"
        
        if not json_path.exists():
            print(f"Error: JSON configuration not found at {json_path}")
            return False
        
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            # Load existing data to preserve portable flag
            self.load_config()
            if self.only_portable and 'only_portable' not in data:
                data['only_portable'] = True
            
            with open(toml_path, 'w', encoding='utf-8') as f:
                toml.dump(data, f)
            
            # Remove JSON file
            json_path.unlink()
            
            print(f"Converted: {json_path} -> {toml_path}")
            return True
            
        except Exception as e:
            print(f"Error converting JSON to TOML: {e}")
            return False
    
    def set_scope(self, scope: Scope) -> None:
        """Set installation scope and calculate shortcut directory."""
        self.scope = scope
        
        if scope == Scope.USER:
            # User scope: %APPDATA%\Microsoft\Windows\Start Menu\opt\
            appdata = os.environ.get('APPDATA', '')
            self.shortcut_dir = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "opt"
        else:
            # Machine scope: %PROGRAMDATA%\Microsoft\Windows\Start Menu\opt\
            programdata = os.environ.get('PROGRAMDATA', '')
            self.shortcut_dir = Path(programdata) / "Microsoft" / "Windows" / "Start Menu" / "opt"
        
        # Create shortcut directory if it doesn't exist
        self.shortcut_dir.mkdir(parents=True, exist_ok=True)


class VariableExpander:
    """Handles variable expansion in strings using $ syntax."""
    
    @staticmethod
    def expand_variables(text: str, metadata: PackageMetadata) -> str:
        """
        Expand variables in a string using $ syntax only.
        
        Variables can be:
        - Package variables: $App, $Icons, $Shortcuts (resolved relative to current junction)
        - Environment variables: $VAR or ${VAR}
        
        Args:
            text: String with variables to expand
            metadata: Package metadata for custom variables
            
        Returns:
            Expanded string with absolute paths
        """
        if not text:
            return text
        
        # First, expand package variables relative to current junction
        # If "current" junction exists, use its resolved target for relative paths
        current_path = metadata.pkg_path / "current"
        base_path = metadata.version_path
        
        # If current junction does not exist, abort
        if not (current_path.exists() and JunctionManager.is_junction(current_path)):
            raise ValueError(f"\"current\" junction does not exist or is not a junction: ({current_path})")
        else:
            # If current junction exists, but it's not pointing to this version, abort
            try:
                target = JunctionManager.get_junction_target(current_path)
            
                if not (target and target.resolve() == base_path.resolve()):
                    raise ValueError(f"\"current\" junction is not pointing to this version ({target} != {base_path})")
            except (OSError, AttributeError) as e:
                raise ValueError(f"Failed to read \"current\" junction target: {current_path}\n Error message: {e}")

        # Set custom package variables with resolved paths
        custom_vars = {
            '$App': str(current_path / "App"),
            '$Icons': str(current_path / "Icons"),
            '$Shortcuts': str(current_path / "Shortcuts")
        }
        
        # First pass: expand package variables
        for var, value in custom_vars.items():
            text = text.replace(var, value)
        # Second pass: expand environment variables with ${VAR} syntax
        def expand_env_var_braces(match):
            var_name = match.group(1)
            return os.environ.get(var_name, '')
        
        text = re.sub(r'\$\{([^}]+)\}', expand_env_var_braces, text)
        
        # Third pass: expand environment variables with $VAR syntax
        # This is more complex as we need to handle word boundaries
        words = text.split()
        expanded_words = []
        
        for word in words:
            # Check if word starts with $ and is followed by alphanumeric/underscore
            if word.startswith('$') and len(word) > 1 and word[1].isalnum():
                var_name = word[1:].split('$')[0]  # Take until next $ or end
                # Find the longest valid environment variable name
                for i in range(len(var_name), 0, -1):
                    if var_name[:i].isidentifier():
                        env_value = os.environ.get(var_name[:i], '')
                        # Replace the variable and keep any suffix
                        word = env_value + word[1 + i:]
                        break
            expanded_words.append(word)
        
        return ' '.join(expanded_words)
    
    @staticmethod
    def expand_dict(data: Dict[str, str], metadata: PackageMetadata) -> Dict[str, str]:
        """Expand variables in all values of a dictionary."""
        return {key: VariableExpander.expand_variables(value, metadata) 
                for key, value in data.items()}


class JunctionManager:
    """Manages NTFS junctions for version switching."""
    
    @staticmethod
    def create_junction(source: Path, target: Path) -> bool:
        """
        Create an NTFS junction.
        
        Args:
            source: Path where junction will be created
            target: Path the junction points to
            
        Returns:
            True if successful, False otherwise
        """
        try:
            # Remove existing junction if it exists
            if source.exists():
                if JunctionManager.is_junction(source):
                    os.unlink(str(source))
                else:
                    return False  # Not a junction, can't overwrite
            
            # Create junction using mklink command
            result = subprocess.run(
                ['cmd', '/c', 'mklink', '/J', str(source), str(target)],
                capture_output=True,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            
            if result.returncode == 0:
                print(f"JUNCTION: created: {source.name} -> {target}")
                return True
            else:
                print(f"JUNCTION error: {result.stderr}")
                return False
                
        except Exception as e:
            print(f"JUNCTION error creating {source}: {e}")
            return False
    
    @staticmethod
    def is_junction(path: Path) -> bool:
        """Check if a path is an NTFS junction."""
        try:
            # Python 3.12+ has os.path.isjunction()
            if hasattr(os.path, 'isjunction'):
                return os.path.isjunction(str(path))
            
            # For older Python versions, we need alternative methods
            # Method 1: Check file attributes using stat
            import stat
            if not os.path.isdir(str(path)):
                return False
            
            try:
                st = os.stat(str(path))
                # Check if it's a reparse point (junction/symlink)
                if not (st.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                    return False
                
                # Now we need to check if it's specifically a junction
                # We can try to read it as a link - junctions can be read with os.readlink in Python 3.8+
                try:
                    target = os.readlink(str(path))
                    return True
                except (OSError, AttributeError):
                    pass
            except (AttributeError, OSError):
                pass
            
            # Method 2: Use dir command as fallback
            result = subprocess.run(
                ['cmd', '/c', 'dir', '/al', str(path.parent)],
                capture_output=True,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            
            if result.returncode == 0:
                import re
                # Look for <JUNCTION> in the output
                junction_name = path.name
                # The output format may vary, so we look for the name and JUNCTION
                lines = result.stdout.split('\n')
                for line in lines:
                    if '<JUNCTION>' in line.upper() and junction_name in line:
                        return True
            
            return False
            
        except Exception:
            return False
    
    @staticmethod
    def get_junction_target(path: Path) -> Optional[Path]:
        """Get the target path of a junction."""
        try:
            # os.readlink works for junctions in Python 3.8+
            target = os.readlink(str(path))
            return normalize_path(target)
        except (OSError, AttributeError):
            # Fallback for older Python or if readlink fails
            pass
        return None
    
    @staticmethod
    def _parse_version_part(part: str) -> Union[int, str]:
        """Parse a version part as integer if possible, otherwise as string."""
        if part.isdigit():
            return int(part)
        return part.lower()  # Case-insensitive comparison
    
    @staticmethod
    def compare_versions(version1: str, version2: str) -> int:
        """
        Compare two version strings with flexible format.
        
        Format: vUPSTREAM.lLOCAL where:
        - UPSTREAM: alphanumeric fields separated by dots
        - LOCAL: integer local revision
        
        Args:
            version1: First version string (e.g., "v1.2.3.l4")
            version2: Second version string
            
        Returns:
            1 if version1 > version2
            -1 if version1 < version2
            0 if equal
        """
        # Extract upstream and local parts
        def split_version(v: str) -> Tuple[List[Union[int, str]], int]:
            # Remove 'v' prefix
            if v.startswith('v'):
                v = v[1:]
            
            # Split local revision
            if '.l' in v:
                upstream_part, local_part = v.rsplit('.l', 1)
                local_rev = int(local_part) if local_part.isdigit() else 0
            else:
                upstream_part = v
                local_rev = 0
            
            # Split upstream version by dots
            upstream_parts = []
            for part in upstream_part.split('.'):
                if part:  # Skip empty parts
                    upstream_parts.append(JunctionManager._parse_version_part(part))
            
            return upstream_parts, local_rev
        
        upstream1, local1 = split_version(version1)
        upstream2, local2 = split_version(version2)
        
        # Compare upstream parts
        for i in range(max(len(upstream1), len(upstream2))):
            up1 = upstream1[i] if i < len(upstream1) else 0
            up2 = upstream2[i] if i < len(upstream2) else 0
            
            # Handle different types
            if type(up1) != type(up2):
                # Convert to string for comparison
                up1 = str(up1)
                up2 = str(up2)
            
            if up1 > up2:
                return 1
            elif up1 < up2:
                return -1
        
        # If upstream equal, compare local revision
        if local1 > local2:
            return 1
        elif local1 < local2:
            return -1
        
        return 0
    
    @staticmethod
    def update_current_junction_if_needed(metadata: PackageMetadata) -> bool:
        """
        Update "current" junction if new version is equal or newer.
        
        Args:
            metadata: Package metadata
            
        Returns:
            True if junction was created/updated, False otherwise
        """
        current_path = metadata.pkg_path / "current"
        
        # Check if current junction exists
        if current_path.exists():
            if not JunctionManager.is_junction(current_path):
                raise ValueError(f"{current_path} exists but is not a junction. Aborting all operations.")
            
            # If we get here and it's a junction, check the target
            if JunctionManager.is_junction(current_path):
                current_target = JunctionManager.get_junction_target(current_path).resolve()
                if not current_target:
                    raise ValueError(f"{current_path} is a junction but its target is not resolvable. Aborting all operations.")
                if not (current_target.parent == metadata.pkg_path and current_target.is_dir()):
                    raise ValueError(f"{current_path} is a junction but its target {current_target} is not a directory under {metadata.pkg_path}. Aborting all operations.")
                
                # Extract version from target path
                current_version = current_target.name
                print(f"\'current\' junction version: {current_version}")
                comparison = JunctionManager.compare_versions(
                    metadata.version_string, 
                    current_version
                )
                
                if comparison >= 0:  # New version is equal or newer
                    return JunctionManager.create_junction(current_path, metadata.version_path)
                else:
                    print(f"JUNCTION: keeping current ({current_version} > {metadata.version_string})")
                    return False
        
        # No current junction exists (or we removed a non-junction file), create it
        return JunctionManager.create_junction(current_path, metadata.version_path)


class ShortcutInstaller:
    """Creates Windows shortcuts in Start Menu."""
    
    @staticmethod
    def _create_shortcut_with_pywin32(shortcut_info: Dict[str, str], metadata: PackageMetadata) -> bool:
        """Create shortcut using pywin32 (preferred method)."""
      
        try:
            # Expand variables in shortcut properties
            expanded = VariableExpander.expand_dict(shortcut_info, metadata)
      
            # Ensure shortcut directory exists
            metadata.shortcut_dir.mkdir(parents=True, exist_ok=True)
            
            shortcut_path = metadata.shortcut_dir / expanded.get('name', '')
            if not shortcut_path.suffix == '.lnk':
                shortcut_path = shortcut_path.with_suffix('.lnk')
      
            # Create shortcut using Windows COM
            shell = win32com.client.Dispatch("WScript.Shell")
            shortcut = shell.CreateShortcut(str(shortcut_path))
            
            # Set shortcut properties
            shortcut.TargetPath = expanded.get('targetPath', '')
            
            if 'arguments' in expanded:
                shortcut.Arguments = expanded['arguments']
            if 'workingDirectory' in expanded:
                shortcut.WorkingDirectory = expanded['workingDirectory']
            if 'iconLocation' in expanded and expanded['iconLocation']!="":
                shortcut.IconLocation = expanded['iconLocation']
            if 'description' in expanded:
                shortcut.Description = expanded['description']
            
            shortcut.Save()
            
            print(f"SHORTCUT: created: {shortcut_path.name}")
            return True
            
        except Exception as e:
            print(f"SHORTCUT error creating {shortcut_info.get('name', 'unknown')}: {e}")
            return False
    
    @staticmethod
    def _create_shortcut_with_powershell(shortcut_info: Dict[str, str], metadata: PackageMetadata) -> bool:
        """Create shortcut using PowerShell (fallback method)."""
        try:
            # Expand variables in shortcut properties
            expanded = VariableExpander.expand_dict(shortcut_info, metadata)
            
            # Ensure shortcut directory exists
            metadata.shortcut_dir.mkdir(parents=True, exist_ok=True)
            
            shortcut_path = metadata.shortcut_dir / expanded.get('name', '')
            if not shortcut_path.suffix == '.lnk':
                shortcut_path = shortcut_path.with_suffix('.lnk')
            
            # Prepare PowerShell command
            target_path = expanded.get('targetPath', '').replace("'", "''")
            arguments = expanded.get('arguments', '').replace("'", "''")
            working_dir = expanded.get('workingDirectory', '').replace("'", "''")
            icon_location = expanded.get('iconLocation', '').replace("'", "''")
            description = expanded.get('description', '').replace("'", "''")
            
            ps_command = f"""
$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut('{shortcut_path}')
$Shortcut.TargetPath = '{target_path}'
$Shortcut.Arguments = '{arguments}'
$Shortcut.WorkingDirectory = '{working_dir}'
$Shortcut.IconLocation = '{icon_location}'
$Shortcut.Description = '{description}'
$Shortcut.Save()
"""
            
            # Execute PowerShell command
            result = subprocess.run(
                ['powershell', '-Command', ps_command],
                capture_output=True,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            
            if result.returncode == 0:
                print(f"SHORTCUT: created (via PowerShell): {shortcut_path.name}")
                return True
            else:
                print(f"SHORTCUT PowerShell error: {result.stderr}")
                return False
                
        except Exception as e:
            print(f"SHORTCUT error creating {shortcut_info.get('name', 'unknown')}: {e}")
            return False
    
    @staticmethod
    def create_shortcut(shortcut_info: Dict[str, str], metadata: PackageMetadata) -> bool:
        """
        Create a Windows shortcut (.lnk file).
        
        Tries pywin32 first, falls back to PowerShell.
        
        Args:
            shortcut_info: Dictionary with shortcut properties
            metadata: Package metadata for variable expansion
            
        Returns:
            True if successful, False otherwise
        """
        if PYWIN32_AVAILABLE:
            return ShortcutInstaller._create_shortcut_with_pywin32(shortcut_info, metadata)
        else:
            print("Warning: pywin32 not available, using PowerShell for shortcut creation")
            return ShortcutInstaller._create_shortcut_with_powershell(shortcut_info, metadata)
    
    @staticmethod
    def install_shortcuts(metadata: PackageMetadata) -> None:
        """Install all shortcuts defined in package configuration."""
        if not PYWIN32_AVAILABLE:
            print("Note: Using PowerShell for shortcut creation (pywin32 not available)")
        
        for shortcut_info in metadata.shortcut:
            ShortcutInstaller.create_shortcut(shortcut_info, metadata)


class EnvironmentVariableManager:
    """Manages Windows environment variables in registry."""
    
    @staticmethod
    def _get_registry_key(scope: Scope):
        """Get the appropriate registry key for environment variables."""
        if scope == Scope.USER:
            return winreg.HKEY_CURRENT_USER, r"Environment"
        else:
            return winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
    
    @staticmethod
    def set_environment_variable(name: str, value: str, scope: Scope, expand: bool = True) -> bool:
        """
        Set an environment variable in Windows registry.
        
        Args:
            name: Variable name
            value: Variable value
            scope: User or Machine scope
            expand: Whether to use REG_EXPAND_SZ (allow variable expansion)
            
        Returns:
            True if successful, False otherwise
        """
        try:
            root, subkey = EnvironmentVariableManager._get_registry_key(scope)
            
            with winreg.OpenKey(root, subkey, 0, winreg.KEY_SET_VALUE) as key:
                reg_type = winreg.REG_EXPAND_SZ if expand else winreg.REG_SZ
                winreg.SetValueEx(key, name, 0, reg_type, value)
                
            print(f"ENVIRONMENT: setting {scope.value} scope: {name} = {value}")
            return True
            
        except PermissionError:
            print(f"ERROR: Insufficient permissions to set {scope.value} environment variable: {name}")
            return False
        except Exception as e:
            print(f"ENVIRONMENT error setting {name}: {e}")
            return False
    
    @staticmethod
    def install_environment_variables(metadata: PackageMetadata) -> None:
        """Install all environment variables defined in package configuration."""
        for env_var in metadata.environment:
            name = env_var.get('Name', '')
            value = env_var.get('Value', '')
            
            if name:
                # Expand variables in the value
                expanded_value = VariableExpander.expand_variables(value, metadata)
                EnvironmentVariableManager.set_environment_variable(
                    name, expanded_value, metadata.scope, expand=True
                )


class PATHManager:
    """Manages PATH environment variable additions."""
    
    @staticmethod
    def get_current_path(scope: Scope) -> List[str]:
        """Get current PATH entries from registry."""
        try:
            root, subkey = EnvironmentVariableManager._get_registry_key(scope)
            
            with winreg.OpenKey(root, subkey, 0, winreg.KEY_READ) as key:
                value, reg_type = winreg.QueryValueEx(key, "Path")
                if reg_type == winreg.REG_EXPAND_SZ or reg_type == winreg.REG_SZ:
                    return [p.strip() for p in value.split(';') if p.strip()]
        except FileNotFoundError:
            # PATH doesn't exist yet
            pass
        except Exception as e:
            print(f"PATH error reading {scope.value} PATH: {e}")
        
        return []
    
    @staticmethod
    def set_path(path_entries: List[str], scope: Scope) -> bool:
        """Set PATH in registry."""
        try:
            # Expand environment variables in path entries
            path_value = ';'.join(path_entries)
        
            root, subkey = EnvironmentVariableManager._get_registry_key(scope)
            
            with winreg.OpenKey(root, subkey, 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, path_value)
            
            return True
            
        except PermissionError:
            print(f"ERROR: Insufficient permissions to set {scope.value} PATH")
            return False
        except Exception as e:
            print(f"PATH error setting {scope.value} PATH: {e}")
            return False
    
    @staticmethod
    def add_to_path(new_entries: List[str], metadata: PackageMetadata) -> bool:
        """
        Add directories to PATH, avoiding duplicates.
        
        Args:
            new_entries: List of directory paths to add (with variables)
            metadata: Package metadata for variable expansion
            
        Returns:
            True if successful, False otherwise
        """
        # Expand variables in new entries
        expanded_new_entries = []
        for entry in new_entries:
            expanded = VariableExpander.expand_variables(entry, metadata)
            # Normalize path
            normalized = os.path.normpath(expanded)
            expanded_new_entries.append(normalized)
        
        # Get current PATH
        current_path = PATHManager.get_current_path(metadata.scope)
        
        # Add new entries, avoiding duplicates
        updated_path = current_path.copy()
        for entry in expanded_new_entries:
            if entry and entry not in updated_path:
                updated_path.append(entry)
                print(f"PATH: adding to {metadata.scope.value} scope: {entry}")
        
        # Only update if there were changes
        if set(current_path) != set(updated_path):
            return PATHManager.set_path(updated_path, metadata.scope)
        
        return True
    
    @staticmethod
    def ensure_bin_in_path(metadata: PackageMetadata) -> bool:
        """
        Ensure bin directory is in PATH.
        
        Args:
            metadata: Package metadata
            
        Returns:
            True if bin directory is in PATH, False otherwise
        """
        if metadata.scope == Scope.USER:
            bin_dir = Path.home() / "bin"
        else:
            # Fix: Ensure we have the root directory (e.g., C:\ instead of C:)
            system_drive = os.environ.get('SYSTEMDRIVE', 'C:')
            # Add backslash if not present (e.g., "C:" -> "C:\")
            if not system_drive.endswith('\\'):
                system_drive += '\\'
            bin_dir = Path(system_drive) / "bin"
        
        # Create bin directory if it doesn't exist
        bin_dir.mkdir(parents=True, exist_ok=True)
        
        # Check if bin directory is already in PATH
        current_path = PATHManager.get_current_path(metadata.scope)
        bin_dir_str = str(bin_dir)
        
        if bin_dir_str not in current_path:
            return PATHManager.add_to_path([bin_dir_str], metadata)
        
        return True


class BinFileCreator:
    """Creates executable wrappers in bin directory."""
    
    @staticmethod
    def get_bin_dir(scope: Scope) -> Path:
        """Get the bin directory for the given scope."""
        if scope == Scope.USER:
            return Path.home() / "bin"
        else:
            return Path(os.environ.get('SYSTEMDRIVE', 'C:')) / "bin"
    
    @staticmethod
    def create_wrapper(wrapper_info: Dict[str, str], metadata: PackageMetadata) -> bool:
        """
        Create an executable wrapper file.
        
        Args:
            wrapper_info: Dictionary with wrapper properties
            metadata: Package metadata for variable expansion
            
        Returns:
            True if successful, False otherwise
        """
        try:
            name = wrapper_info.get('name', '')
            content = wrapper_info.get('content', '')
            
            if not name:
                return False
            
            # Expand variables in content
            expanded_content = VariableExpander.expand_variables(content, metadata)
            
            # Get bin directory and ensure it exists
            bin_dir = BinFileCreator.get_bin_dir(metadata.scope)
            bin_dir.mkdir(parents=True, exist_ok=True)
            
            # Create wrapper file
            wrapper_path = bin_dir / name
            
            with open(wrapper_path, 'w', encoding='utf-8') as f:
                f.write(expanded_content)
            
            print(f"BIN: created: {wrapper_path}")
            return True
            
        except Exception as e:
            print(f"BIN error creating {wrapper_info.get('name', 'unknown')}: {e}")
            return False
    
    @staticmethod
    def install_wrappers(metadata: PackageMetadata) -> None:
        """Install all bin wrappers defined in package configuration."""
        for wrapper_info in metadata.bin:
            BinFileCreator.create_wrapper(wrapper_info, metadata)


class PackageManager:
    """Main package manager orchestrating the installation process."""
    
    def __init__(self, scope: Scope = Scope.USER, pause: bool = False,
                 no_autoupdate_config: bool = False):
        """
        Initialize package manager.
        
        Args:
            scope: Installation scope (User or Machine)
            pause: Interactive pause at completion
            no_autoupdate_config: Abort if config is inconsistent instead of auto-updating
        """
        self.scope = scope
        self.pause = pause
        self.no_autoupdate_config = no_autoupdate_config
        
        # Warn about missing pywin32
        if not PYWIN32_AVAILABLE:
            print("Warning: pywin32 not available. Shortcuts will be created using PowerShell.")
            print("For better performance, install pywin32: pip install pywin32")
        
        # Check for admin rights if Machine scope
        if scope == Scope.MACHINE and not self._is_admin():
            print("ERROR: Machine scope requires administrator privileges.")
            print("Please run as administrator.")
            sys.exit(1)
    
    def _is_admin(self) -> bool:
        """Check if running with administrator privileges."""
        try:
            import ctypes
            return ctypes.windll.shell32.IsUserAnAdmin()
        except Exception:
            return False
    
    def install(self, package_path: Path) -> None:
        """
        Main installation process.
        
        Args:
            package_path: Path to package directory (version directory or "current" junction)
        """
        print(f"\n{'='*60}")
        print(f"gu-opt-pkg: Package Manager")
        print(f"Scope: {self.scope.value}")
        print(f"{'='*60}\n")
        
        # Extract metadata
        try:
            metadata = PackageMetadata(package_path)
            metadata.set_scope(self.scope)
            config_data = metadata.load_config()  # Changed to capture returned datametadata.load_config()
        except Exception as e:
            print(f"ERROR: Failed to parse package metadata: {e}")
            self._pause()
            return
        
        print(f"Package: {metadata.name}")
        print(f"Version: {metadata.version_string}")
        print(f"Path: {metadata.version_path}")
        print(f"only_portable: {metadata.only_portable}\n")
        
        # Check metadata consistency
        inconsistencies = metadata.check_metadata_consistency(config_data)
        if inconsistencies:
            if self.no_autoupdate_config:
                print("ERROR: Configuration inconsistencies detected and --no-autoupdate-config is enabled:")
                for msg in inconsistencies:
                    print(f"  - {msg}")
                print("\nAborting installation. Please fix the configuration manually.")
                self._pause()
                return
            else:
                print("WARNING: Configuration inconsistencies detected:")
                for msg in inconsistencies:
                    print(f"  - {msg}")
                print("\nAuto-updating configuration file to match directory structure...")
                metadata.update_config(config_data)
                print("Configuration updated successfully.\n")
                
        # Check if portable package is being installed as Machine
        if metadata.only_portable and self.scope == Scope.MACHINE:
            print("ERROR: Only_Portable packages cannot be installed system-wide.")
            print("Please use User scope for Only_Portable packages.")
            self._pause()
            return
        
        # Check if installing from "current" junction
        if JunctionManager.is_junction(package_path):
            print("Installing from 'current' junction (skipping junction management)")
            self._install_components(metadata)
        else:
            # Manage "current" junction
            print("Managing 'current' junction...")
            junction_updated = JunctionManager.update_current_junction_if_needed(metadata)
            
            # Only install components if junction was created/updated
            if junction_updated or metadata.is_current:
                print("\nInstalling components...")
                self._install_components(metadata)
            else:
                print("\nSkipping component installation (newer version already installed)")
        
        print(f"\n{'-'*60}")
        print("Installation complete!")
        print(f"{'-'*60}")
        
        self._pause()
    
    def _install_components(self, metadata: PackageMetadata) -> None:
        """Install all package components."""
        # Install shortcuts
        if metadata.shortcut:
            print("\nCreating shortcuts...")
            ShortcutInstaller.install_shortcuts(metadata)
        
        # Install environment variables
        if metadata.environment:
            print("\nSetting environment variables...")
            EnvironmentVariableManager.install_environment_variables(metadata)
        
        # Ensure bin directory is in PATH
        print("\nManaging PATH...")
        PATHManager.ensure_bin_in_path(metadata)
        
        # Add package directories to PATH
        if metadata.path:
            PATHManager.add_to_path(metadata.path, metadata)
        
        # Install bin wrappers
        if metadata.bin:
            print("\nCreating executable wrappers...")
            BinFileCreator.install_wrappers(metadata)
    
    def update_config(self, package_path: Path) -> None:
        """
        Only update configuration from directory structure.
        
        Args:
            package_path: Path to package directory
        """
        
        try:
            metadata = PackageMetadata(package_path)
            metadata.load_config()  # Load existing if any
            metadata.update_config()
            
            print(f"Updated configuration for {metadata.name} {metadata.version_string}")
            
        except Exception as e:
            print(f"ERROR: Failed to update configuration: {e}")
        
        self._pause()
    
    def convert_json_to_toml(self, package_path: Path) -> None:
        """
        Convert JSON configuration to TOML.
        
        Args:
            package_path: Path to package directory
        """
        try:
            metadata = PackageMetadata(package_path)
            
            if metadata.convert_json_to_toml():
                print("Successfully converted JSON to TOML")
            else:
                print("Failed to convert JSON to TOML")
                
        except Exception as e:
            print(f"ERROR: Failed to convert JSON to TOML: {e}")
        
        self._pause()
    
    def _pause(self) -> None:
        """Pause at completion unless --pause flag is set."""
        if self.pause:
            print("\nPress any key to continue...")
            try:
                import msvcrt
                msvcrt.getch()
            except ImportError:
                # Fallback for non-Windows (though this is Windows-only)
                input("Press Enter to continue...")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description='Local Package Manager for Windows (gu-opt-pkg)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                       # Install in User scope from current directory
  %(prog)s --scope Machine       # Install system-wide (requires admin)
  %(prog)s --action UpdateConfig # Only update configuration file
  %(prog)s --action ConvertJSONToTOML  # Convert JSON config to TOML
  %(prog)s --pause            # Disable completion pause
        """
    )
    
    parser.add_argument(
        '--scope',
        choices=[s.value for s in Scope],
        default=Scope.USER.value,
        help='Installation scope: User (per-user) or Machine (system-wide)'
    )
    
    parser.add_argument(
        '--action',
        choices=[a.value for a in Action],
        default=Action.INSTALL.value,
        help='Action to perform'
    )
    
    parser.add_argument(
        '--pause',
        action='store_true',
        help='Disable interactive pause at completion'
    )
    
    parser.add_argument(
        'path',
        nargs='?',
        default='.',
        help='Path to package directory (default: current directory)'
    )
    
    parser.add_argument(
        '--no-autoupdate-config',
        action='store_true',
        default=False,
        help='Abort operation if configuration is inconsistent with directory structure (instead of auto-updating)'
    )

    args = parser.parse_args()
    
    # Convert string to enum
    scope = Scope(args.scope)
    action = Action(args.action)
    
    # Create package manager
    manager = PackageManager(scope=scope, pause=args.pause)
    
    # Resolve package path
    package_path = Path(args.path).resolve()
    
    # Execute requested action
    if action == Action.INSTALL:
        manager.install(package_path)
    elif action == Action.UPDATE_CONFIG:
        manager.update_config(package_path)
    elif action == Action.CONVERT_JSON_TO_TOML:
        manager.convert_json_to_toml(package_path)
    elif action == Action.COMPRESS:
        raise NotImplementedError("Compress action not yet implemented")
    else:
        print(f"Unknown action: {action}")
        sys.exit(1)


if __name__ == "__main__":
    main()
