"""Cover Windows API bindings used for environment-change notifications.

The user32 boundary is mocked while ctypes and the public notification helper
are real. Registry writes, Windows message delivery, and timeout behavior are
outside this suite's scope.
"""

from __future__ import annotations

import ctypes
from types import SimpleNamespace
from unittest import mock

from gupkg.windows import broadcast_environment_change


def test_environment_notification_uses_a_portable_pointer_sized_result() -> None:
    """Environment notifications work without the optional wintypes ULONG_PTR alias."""
    send_message_timeout = mock.Mock(return_value=1)
    user32 = SimpleNamespace(SendMessageTimeoutW=send_message_timeout)

    with mock.patch.object(
        ctypes, "windll", SimpleNamespace(user32=user32), create=True
    ):
        broadcast_environment_change()

    assert send_message_timeout.argtypes[-1]._type_ is ctypes.c_size_t
    assert send_message_timeout.call_args.args[1] == 0x001A
