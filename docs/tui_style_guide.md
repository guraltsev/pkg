# Minimal list-first TUI style guide

This guide defines the interaction and visual language used by `pkg tui`. It
is intentionally suitable for other terminal applications that need to expose
substantial capability without becoming a dashboard, a pseudo-GUI, or a
collection of terminal art.

The central rule is simple: a terminal UI should look and behave like a short,
well-ordered list of decisions. The terminal already supplies a window, a
cursor, scrolling, and keyboard input. Do not redraw those concepts as boxes,
panels, cards, toolbars, or decorative separators.

## Goals

Use this style when the application:

- has a small number of actions and settings;
- is primarily keyboard-driven;
- must work in narrow, short, resized, or remote terminals;
- should expose the same operations as an existing command line;
- benefits from showing a small amount of contextual status before an action;
- should be readable without learning a visual design system.

The desired result is calm and direct. A user should see the current package
or object, a single list of available choices, and no competing visual chrome.

## Visual rules

### Use one list per decision

Show actions and settings with one selectable list. The selected item is the
only necessary indication of focus. Do not place the list inside a panel or
add a parallel row of buttons.

For an operation screen, put the default action first, then settings:

```text
Run
--- Settings ---
Package path: current directory
Installation Scope: Local
Skip checksum verification: off
```

The separator is content, not decoration. It tells the reader where execution
ends and editable state begins. Keep it literal and short: `--- Settings ---`.
Do not add blank rows before or after it.

### Do not draw frames

Avoid all of the following unless a terminal protocol genuinely requires them:

- ASCII or Unicode box borders;
- bordered form fields, cards, panels, dialogs, and tables;
- headers and footers used only as application chrome;
- button-like widgets for ordinary actions;
- large title banners, logos, or repeated product names;
- spacer rows used only to make a screen feel less empty.

The selected list row, short section marker, and plain text status are enough.
If a widget library renders borders by default, explicitly turn borders,
outlines, and background panels off in the application stylesheet.

### Keep density intentional

Do not add vertical breathing room merely because graphical applications do.
One line per piece of information is the default. A list should not need a
large terminal to show its useful choices.

Use a line only when it changes the user's decision:

- package title and version;
- installed version;
- one-line description;
- metadata conflict warning;
- action or setting;
- command result.

Avoid standalone instructional text when the list behavior is conventional.
Use short labels and predictable keys instead.

### Prefer words to status glyphs

Use `on` and `off`, `Local` and `Machine`, or `unavailable`, rather than
bracketed checkbox art such as `[x]` and `[ ]`. Text states remain readable in
monochrome terminals, screen readers, copied output, and low-quality remote
sessions.

Use color only to reinforce meaning already stated in words. A warning must
begin with `Warning:` even if it is rendered in a warning color.

## Information hierarchy

### Context appears before choices

When a screen operates on a package, file, project, or other concrete object,
show its identity before the list:

```text
<name> <version>  Installed: <active version or not installed>
<one-line description>
Warning: metadata conflicts with the directory name.
```

Derive the identity from the authoritative source, not from a possibly stale
display field. For `pkg`, directory identity owns the package name and version;
`pkg.toml` metadata is checked against it rather than trusted over it.

The warning line appears only when there is a conflict. Do not reserve a blank
line for absent warnings.

### Descriptions stay one line

Descriptions should occupy one terminal line. Clip overflow with an ellipsis.
If additional text exists, make the description line clickable or selectable
and open a plain full-description view. Do not wrap a long description above a
short action list, because it moves the actual choices out of view in small
terminals.

### Results are a separate view

An operation may produce many lines of command output. Run it on a dedicated,
scrollable result view rather than inserting output between settings or
replacing list rows with logs.

The result view contains only:

1. the command summary;
2. a short running/completed status;
3. the scrollable output.

Do not add a button bar or footer. Returning to the originating list is enough.

## Interaction rules

### Execution is the default

Every operation list begins with `Run`, and `Run` is highlighted and focused
when the screen opens. Users who accept the detected/default settings should
be able to press Enter immediately.

Settings must be visible and editable, but they must not become the default
focus or require a preliminary settings submenu.

### Settings edit in place

Boolean settings toggle between `on` and `off` when selected. A finite choice,
such as scope, cycles through its permitted values when selected. If a choice
is not permitted, show that fact directly in the row, for example:

```text
Installation Scope: Local (Machine unavailable)
```

Do not open a submenu simply to change a boolean or a two-value choice.

Text settings, such as paths, may temporarily open a minimal text-entry view.
That view should contain only the entry itself. Pressing Enter saves and
returns to the same list; Escape discards and returns. It is an editor, not a
new navigation section.

### Arrow keys are the primary navigation

The up and down arrows move through the list. Enter activates the selected
row. Do not require Tab to discover or traverse sections.

Use left and right only when a setting has a meaningful horizontal choice. Do
not make users memorize chorded shortcuts for ordinary navigation.

Navigation policy:

| Context | Escape | `q` |
| --- | --- | --- |
| Main action list | Exit the application | Exit the application |
| Action/settings list | Return to the main list | Exit the application |
| Text editor, output, or description | Return one level | Exit the application |

This gives Escape a predictable “go back unless already at home” meaning while
still allowing a quick exit from the main screen.

## Responsive behavior

Terminal dimensions are not stable. Users resize windows, attach through SSH,
change fonts, and run inside split panes. A design that depends on a particular
height or width is not a terminal design.

Follow these rules:

- never give a fixed height to the whole form;
- let the option list consume remaining height and scroll naturally;
- make long output scrollable rather than attempting to fit it;
- keep descriptions to one clipped line;
- avoid side-by-side form layouts that wrap unpredictably;
- avoid permanently visible help text and footers;
- keep each operation on one list so that focus remains meaningful after a
  resize.

If a small terminal cannot show every setting at once, the list should scroll.
The selected row must remain visible. Never hide settings below a fixed-height
panel or make an operation unreachable because a form overflowed.

## Textual implementation notes

Textual is useful here as a terminal event loop and selectable-list renderer,
not as a reason to build a desktop-style interface in a terminal.

Apply a minimal stylesheet like this:

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

Important implementation choices:

- Use `OptionList` for both actions and settings.
- Represent disabled choices as disabled list rows, not hidden logic.
- Rebuild or refresh option prompts after an in-place setting changes while
  retaining the selected row.
- Keep option identifiers separate from displayed text so labels can change
  without changing command behavior.
- Run long-lived commands in a worker or child process so the interface stays
  responsive and terminal output does not corrupt Textual rendering.
- Capture command output and display it in the result view rather than writing
  directly to the terminal under the application.
- Keep optional runtime dependencies provisioned by the application, not by a
  manual prerequisite the user must remember.

## Review checklist

Before shipping a list-first TUI, verify the following.

### Appearance

- Is every frame, outline, footer, button bar, and decorative spacer removed?
- Is each screen understandable from its plain text alone?
- Is `Run` first and initially selected?
- Are all settings visible in the same list as the action?
- Are warnings explicit words, not color-only indicators?

### Navigation

- Can a user move through every list row with Up/Down and activate it with
  Enter?
- Does Escape exit only from the main list and go back everywhere else?
- Does editing a text value return to the original list without losing other
  settings?
- Can the user run an action without entering a settings submenu?

### Responsive behavior

- Does the action remain usable in a narrow terminal and a short split pane?
- Does long text clip or scroll instead of expanding fixed layouts?
- Does command output have its own scrollable view?
- Does resizing preserve the visible focused row and avoid overlapping fields?

### Semantics

- Does displayed identity come from the authoritative object or filesystem?
- Are conflicting metadata and unavailable choices visible to the user?
- Are disabled choices visibly unavailable rather than silently ignored?
- Does the TUI delegate to the same command/API behavior as the noninteractive
  interface?
