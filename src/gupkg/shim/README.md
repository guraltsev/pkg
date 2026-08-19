# exe-shim

`exe-shim` turns a TOML file into a small, native Windows launcher. Copy a
launcher executable to the command name you want, place a matching
`.config.toml` file beside it, and the launcher starts the configured target
with predictable arguments, environment changes, working directory, and exit
code handling. It does not invoke a shell, so it avoids shell-specific quoting
and command-injection behavior.

For example, `gs.exe` reads `gs.config.toml`. It can launch Git with fixed
arguments, append arguments supplied by the caller, and return Git's exit code.

## Use a launcher

1. Choose a launcher (see [Console and GUI launchers](#console-and-gui-launchers)).
2. Copy it to the command name you want to provide.
3. Create `<command>.config.toml` next to that executable.
4. Add the directory containing the executable to `PATH` if you want to invoke
   it by name.

For a `gs` command installed in `C:\Bin`:

```bat
copy shim-console.exe C:\Bin\gs.exe
```

Create `C:\Bin\gs.config.toml`:

```toml
target = "C:\\Program Files\\Git\\bin\\git.exe"

[[argument]]
value = "status"

[[argument]]
value = "-uno"
```

Now `gs -s` is equivalent to:

```powershell
& 'C:\Program Files\Git\bin\git.exe' status -uno -s
```

The launcher always reads the configuration next to itself, rather than from
the current directory. Relative `target`, `working_dir`, and `path_prepend`
paths are resolved from that configuration directory.

### Create a shim from the command line

`make-shim.cmd` creates a launcher and a commented configuration
file in the current directory. Its target is the required first argument:

```bat
make-shim.cmd "%LOCALAPPDATA%\Programs\Example\tool.exe" --name=tool --type=cli --elevate=false
```

Options are `--name=NAME`, `--type=cli|gui`, `--elevate=true|false`,
`--forward-arguments=true|false`, and `--working-dir=PATH`. The defaults are
the target's basename, `cli`, `false`, and `true`, respectively. The generated
`.config.toml` includes commented examples for fixed arguments, child-only
environment variables, variable removal, and `PATH` prefixes. It refuses to
overwrite an existing executable or configuration file.

## Console and GUI launchers

Build output contains two launchers with identical TOML configuration behavior:

| Launcher | Use it for | Behavior |
| --- | --- | --- |
| `shim-console.exe` | Command-line and terminal tools | Attaches to the calling console. The target inherits terminal behavior, including Ctrl+C handling. |
| `shim-gui.exe` | Desktop applications opened from Explorer, shortcuts, or file associations | Uses the Windows GUI subsystem, so it does not create a console window when launched outside a terminal. |

Use the console launcher for console targets. A GUI launcher can start a
console program, but its output and interactive console behavior will not be
available when it is launched from Explorer. Conversely, starting a GUI target
through the console launcher from a terminal is valid, but the launcher remains
attached to that terminal while it waits for the target to exit.

## Common use cases

### Create a Git shortcut

The `gs` example above supplies `status -uno` and forwards additional caller
arguments. `gs -s` therefore adds `-s` after those fixed arguments.

### Run a project-local tool from any directory

This launcher is relocatable because its paths are relative to its TOML file:

```toml
target = "bin\\real-tool.exe"
working_dir = "work"
forward_arguments = false

[[argument]]
value = "serve"
```

Copy the directory containing `tool.exe`, `tool.config.toml`, `bin`, and
`work` together. `tool.exe` always runs `bin\real-tool.exe` in `work`, and it
always supplies `serve`; caller arguments are deliberately ignored.

### Set a child-only environment

Use this for tools that need configuration without changing the parent shell:

```toml
target = "%LOCALAPPDATA%\\Programs\\Example\\tool.exe"
path_prepend = ["tools"]
remove_environment = ["VIRTUAL_ENV"]

[environment]
RUST_LOG = "info"
TOOL_CACHE = "%LOCALAPPDATA%\\Example\\cache"
```

Windows `%NAME%` references expand from the launcher's inherited environment.
The child receives the added `tools` directory first on `PATH`, has
`VIRTUAL_ENV` removed, and receives the two configured values. These changes
also apply to its descendants, never to the shell that started the launcher.

### Start an application with elevation

Use a GUI launcher for a desktop application that must request UAC elevation:

```toml
target = "C:\\Program Files\\Example\\Admin App\\admin-app.exe"
elevate = true
```

Windows shows the UAC prompt. If elevation is declined or fails, the launcher
returns a non-zero exit code and does not start the target unelevated.

## Configuration reference

`target` is required. All other settings are optional:

```toml
target = "%LOCALAPPDATA%\\Programs\\Example\\tool.exe"
forward_arguments = true # Defaults to true.
elevate = false           # Defaults to false.
working_dir = "project"
remove_environment = ["VIRTUAL_ENV"]
path_prepend = ["tools"]

[environment]
RUST_LOG = "info"

[[argument]]
value = "--color=always"
```

Configured `[[argument]]` entries appear before caller arguments.
`forward_arguments = false` suppresses caller arguments. `remove_environment`
and `[environment]` may not name the same variable (Windows variable names are
case-insensitive). Invalid TOML, unknown keys, invalid values, and references
to unset `%NAME%` variables cause the launcher to fail before starting a
target. Configuration files must be valid UTF-8.

## Build

The project requires a Windows C++23 toolchain, CMake 3.23 or newer, and Conan
2. Dependencies, including `fmt` and `tomlplusplus`, are provided by Conan.
From a Windows command prompt, an MSYS2 UCRT64 shell, or a Visual Studio
developer prompt:

```bat
build.cmd
```

`build.cmd` creates a default Conan profile when necessary, installs
dependencies, configures CMake, and builds the project. To run those steps
individually:

```bat
conan profile detect --force
conan install . --output-folder=build --build=missing
cmake --preset conan-release
cmake --build --preset conan-release
```

Before running the script from a normal Windows command prompt, set one of
these environment variables to locate the UCRT64 toolkit:

```bat
set "EXE_SHIM_UCRT64_ROOT=C:\opt\msys\current\App\ucrt64"
build.cmd
```

Alternatively, set `MSYS2_INSTALL_PATH` to the MSYS2 installation directory;
the script uses its `ucrt64` subdirectory. If neither variable is set,
`build.cmd` requires both `g++` and `mingw32-make` to already be on `PATH`.

Conan and CMake generate all build-system files, intermediate objects, static
libraries, and test helpers in `build/`. Final distribution artifacts are
written directly to `artifacts/`: `shim-console.exe`, `shim-gui.exe`, and
`make-shim.cmd`.

## Repository layout

- `src/` contains the shared launcher implementation and its console and GUI
  entry points.
- `tests/` contains the Python integration suite, its TOML fixtures, and
  test-only helper executables in `tests/helpers/`.
- `docs/` contains project guidance and design notes.
- `build/` is generated CMake/Conan build output and is not committed.
- `artifacts/` contains final distribution artifacts and is not committed.

## Testing

Run the automated integration suite after building:

```bat
ctest --preset conan-release --output-on-failure
```

The suite uses Python's standard library and exercises configuration lookup,
arguments, relative paths, environment edits, validation failures, and exit
code propagation. For checks that require Explorer, console behavior, or UAC,
follow the [manual testing checklist](tests/MANUAL_TESTING.md).

## Process behavior

The launcher waits for the target and returns its exit code. Non-elevated
launches run the target in a Windows job object, so child processes are
terminated when the launcher is terminated. Console launchers suppress the
launcher's default Ctrl+C handling so the target can receive console control
events.

## License

SPDX-License-Identifier: MIT OR Unlicense
