## Update coordinator

Automatic-update behavior remains in `src/pkg.py` and follows a deliberately
small coordinator: resolve the active package, validate its `[update]` table,
acquire the package-root lock, check for a candidate, stage a complete version
under `.pkg/work`, validate it, atomically commit it, and activate it through
the established install workflow. Check hooks and unpack hooks are imported
from `pkg.local/` as trusted in-process Python extensions; they are never
executed as shell commands.

Update work, locks, timing state, and receipts are manager-owned data beneath
the package root's `.pkg/` directory. A finalized version directory contains
only package-authored files and its completed `App/` payload.
