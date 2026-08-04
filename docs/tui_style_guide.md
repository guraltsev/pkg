# Minimal list-first TUI guide

This guide defines the interaction, visual language, and implementation
boundaries for `gupkg tui`. It is also intended as a reusable approach for small
Python terminal applications: apply the principles to the application's needs,
rather than copying its screens verbatim.

## Purpose and fit

A small TUI is a decision surface, not a desktop application reproduced in a
terminal. The terminal already provides a window, cursor, scrolling, and
keyboard input. The application need only make the current context, available
choices, and result unambiguous.

Use this style when the application:

- has a modest number of operations and settings;
- is primarily keyboard-driven;
- must work in narrow, short, resized, or remote terminals;
- exposes the same operations as an existing command line or Python API; and
- benefits from a little contextual status before an action.

It is less suitable for comparing many records, monitoring several live
signals, manipulating spatial data, or dense text editing. Those tasks may
justify tables, panes, persistent help, or a richer application model.

The central rule is simple: present a short, well-ordered list of decisions.
The desired result is calm and direct: current object, one list of choices, and
no competing visual chrome.

## Visual language

### Use one list per decision

Show actions and settings in one selectable list. The selected item is the
only necessary indication of focus. Do not put the list inside a panel or add a
parallel row of buttons.

For an operation screen, put the default action first, then its settings:

```text
Run
--- Settings ---
Package path: current directory
Installation Scope: Local
Skip checksum verification: off
```

The separator is content, not decoration: it distinguishes execution from
editable state. Keep it literal and short, and do not add blank rows around it.

### Do not draw frames

Avoid the following unless a terminal protocol genuinely requires them:

- ASCII or Unicode borders;
- bordered form fields, cards, panels, dialogs, and tables;
- headers and footers used only as application chrome;
- button-like widgets for ordinary actions;
- large title banners, logos, or repeated product names; and
- spacer rows used only to make a screen feel less empty.

Plain context, a selected list row, short section markers, and text status are
enough. If a widget library draws borders, outlines, or background panels by
default, turn them off explicitly.

### Keep density intentional

Use one line per useful piece of information. Add a line only when it changes
the user's decision: object identity, installed version, one-line description,
metadata warning, action, setting, or command result. Avoid standalone
instructions when the list behavior is conventional.

Prefer words to status glyphs: use `on` and `off`, `Local` and `Machine`, or
`unavailable`, rather than checkbox art. Color may reinforce meaning, but it
must never be the only signal: write `Warning:` even when the line is colored.

The test is simple: a copied, unstyled, monochrome rendering should still make
the hierarchy and every important state clear.

## Information hierarchy

### Put context before choices

When a screen operates on a package, file, project, or other concrete object,
show its identity before the list:

```text
<name> <version>  Installed: <active version or not installed>
<one-line description>
Warning: metadata conflicts with the directory name.
```

Use the authoritative source for identity, not a potentially stale display
field. In `gupkg`, directory identity owns the package name and version;
`pkg.toml` metadata is checked against it rather than trusted over it. Show a
warning only when the conflict exists; do not reserve blank space for it.

### Reveal detail progressively

Decision-relevant state stays visible; explanatory detail is available on
demand; high-volume output gets its own view. Descriptions occupy one terminal
line and clip overflow with an ellipsis. When more text exists, make that line
selectable or clickable and open a plain, scrollable full-description view.
Do not wrap long descriptions above a short action list.

An operation's output belongs in a dedicated scrollable result view rather than
between settings or in place of list rows. That view contains only:

1. a command summary;
2. a short textual running or completion status; and
3. scrollable output.

Do not add a button bar or footer; returning to the originating list is enough.

## Screen and interaction model

A new screen is justified when the user's mode of attention changes, not merely
because the program has another data type. Most compact applications need only:

1. a home view, which establishes context and lists operations;
2. an operation view, which shows `Run` and its settings;
3. a temporary value editor for text that cannot comfortably change in a row;
   and
4. a result view for progress, completion status, and output.

```text
context and operations
        |
        v
Run and settings  <-->  temporary value editor
        |
        v
status and output
```

Avoid screens that merely introduce another screen. Booleans do not need
dialogs, and two choices do not need submenus.

### Make execution immediate

Every operation list begins with `Run`; it is highlighted and focused when the
screen opens. Users who accept the detected settings should be able to press
Enter immediately. Settings remain visible below it for review and adjustment,
but must not become the default focus or require a preliminary settings menu.

Defaults come from the same domain logic as noninteractive calls. The TUI may
explain a default but must not quietly invent a different one.

### Edit settings in place

Selecting a boolean toggles `on` and `off`. Selecting a finite choice cycles
through its permitted values. If a value is not permitted, show it directly:

```text
Installation Scope: Local (Machine unavailable)
```

Text settings may open a minimal editor containing only the entry. Enter saves
and returns to the same list; Escape discards and returns. The editor is not a
navigation section. After an in-place change, retain the selected row so focus
does not jump.

### Keep navigation uniform

Up and Down move through the list, and Enter activates the selected row. Do not
require Tab, mouse input, or memorized shortcuts for ordinary use. Left and
right are appropriate only for a setting with a meaningful horizontal choice.

| Context | Escape | `q` |
| --- | --- | --- |
| Main action list | Exit the application | Exit the application |
| Action/settings list | Return to the main list | Exit the application |
| Text editor, output, or description | Return one level | Exit the application |

This makes Escape mean “go back unless already at home” while retaining a quick
exit from anywhere.

## Responsive behavior

Terminal dimensions are unstable: users resize windows, connect through SSH,
change fonts, and work in split panes. Build responsive behavior into the
structure rather than a collection of breakpoints:

- use one vertical reading order;
- never assign the whole form a fixed height;
- let the option list take the remaining height and scroll naturally;
- keep summaries to one clipped line and offer detail separately;
- make long output scrollable;
- avoid side-by-side forms and permanently visible help or footers; and
- keep each operation in one list so focus survives a resize.

When a terminal is too short, show fewer rows at once; never hide a setting,
make an operation unreachable, overlap text, or change the navigation model.
The selected row must remain visible.

## Preserve application semantics

The TUI is another way to invoke the application's behavior, not a second
application. Keep domain rules, validation, defaults, and side effects in the
existing Python API or CLI path. The TUI gathers intent, translates it to that
interface, and presents the result.

Prefer a shared application function when one exists. If the CLI is the stable
integration boundary, launch it as a child process with an argument list;
neither duplicate its parser nor execute shell text. This keeps interactive and
scripted behavior aligned and makes command construction inspectable.

Keep framework code at the presentation edge:

```text
domain or CLI layer
    validates inputs and performs operations

presentation adapter
    converts current state into labels and command arguments

TUI screens
    own temporary interaction state and dispatch user intent
```

Keep stable option identifiers separate from displayed labels. Store state at
the narrowest useful level: application context in the home/application object,
operation flags in the operation view, and unfinished edits in the editor.
Importing the core package or using its CLI noninteractively should not require
the TUI framework unless the interface is inseparable from installation.

## Long-running work

Filesystem, subprocess, and network work must not block the terminal event
loop. Use a framework worker, asynchronous task, background thread, or child
process as appropriate. Capture command output rather than allowing a child to
write through the active renderer.

Open the result view before work starts, then transition through explicit text
states such as `Running`, `Completed`, or `Failed`. Rendering ownership remains
clear: the TUI owns the terminal; application operations produce data for it to
display.

## Textual implementation notes

Textual is useful here for event handling, selectable lists, scrolling, and
terminal restoration—not as a reason to build a desktop-style interface.

```tcss
Screen { padding: 0; }
Label, Static { margin: 0; }
OptionList, Input, VerticalScroll {
    background: transparent;
    border: none;
    outline: none;
}
OptionList, VerticalScroll { height: 1fr; }
#description {
    height: 1;
    overflow: hidden;
    text-overflow: ellipsis;
}
```

- Use `OptionList` for actions and settings.
- Render unavailable choices as disabled rows rather than hiding them.
- Refresh prompts after an in-place setting change while retaining selection.
- Run commands outside the UI loop and show captured output in the result view.
- Provision optional runtime dependencies through the application rather than a
  prerequisite the user must remember.

## Review checklist

Before shipping, verify the following:

- Every decorative frame, outline, footer, button bar, and spacer is removed.
- The screen makes sense as plain monochrome text.
- The authoritative object identity, conflicts, and unavailable choices are
  visible.
- `Run` is first and initially selected; all settings share its list.
- Up/Down and Enter reach and activate every row; Escape has the documented
  back/exit behavior.
- Text edits return to their original list without losing other settings.
- The same command/API semantics, validation, and defaults serve TUI and CLI.
- Narrow and short terminals clip or scroll rather than hide controls or
  overlap fields.
- Long commands remain responsive and their output has a separate scrollable
  view.

When adapting this guide to another Python application, start by answering:
what authoritative context must appear first; what is the smallest current list
of decisions; which action is immediately executable with defaults; which
values need an editor; and which content needs clipping, scrolling, or a
dedicated detail view? Choose Textual, prompt_toolkit, urwid, or another
framework only after that interaction model is clear.
