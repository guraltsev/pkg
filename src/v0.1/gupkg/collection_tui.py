"""Present a collection inventory and enter the established package interface.

The collection screen deliberately exposes read-oriented package status and a
single selection action.  Package mutations remain in the existing package
screen, which keeps collection mode from implying bulk installation or update
activation.
"""

from __future__ import annotations

from .collection import Inventory


def run_collection_tui(inventory: Inventory) -> int:
    """Run the collection selector and open the selected package UI.

    Parameters
    ----------
    inventory : Inventory
        Collection result to display in deterministic selector order.

    Returns
    -------
    int
        Status after the collection or selected package interface closes.
    """
    from textual.app import App, ComposeResult
    from textual.widgets import Footer, Header, OptionList, Static
    from textual.widgets.option_list import Option

    class CollectionApp(App[str | None]):
        """Display selectable package rows for one collection inventory."""

        BINDINGS = [("q", "quit", "Quit")]

        def compose(self) -> ComposeResult:
            """Compose the compact inventory and its package selectors."""
            status = "complete" if inventory.complete else "incomplete"
            yield Header()
            yield Static(f"{inventory.root} ({status})")
            yield OptionList(
                *(Option(package.selector, id=package.selector) for package in inventory.packages)
            )
            yield Footer()

        def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
            """Return the selected canonical selector to the dispatcher."""
            self.exit(event.option.id)

    selected = CollectionApp().run()
    if not selected:
        return 0
    package = next(package for package in inventory.packages if package.selector == selected)
    from .tui import run_tui

    return run_tui(str(package.root))
