"""Regressões do encerramento cooperativo dos workers persistentes."""

from __future__ import annotations

import signal
import unittest
from unittest.mock import patch

from platform_core.worker_runtime import graceful_worker_shutdown


class GracefulWorkerShutdownTests(unittest.TestCase):
    def test_sigterm_requests_stop_and_previous_handlers_are_restored(self):
        handlers: dict[int, object] = {}

        def fake_getsignal(candidate):
            return f"previous-{candidate}"

        def fake_signal(candidate, handler):
            handlers[candidate] = handler

        with patch("platform_core.worker_runtime.signal.getsignal", side_effect=fake_getsignal), patch(
            "platform_core.worker_runtime.signal.signal", side_effect=fake_signal
        ):
            with graceful_worker_shutdown() as stopping:
                handlers[signal.SIGTERM](signal.SIGTERM, None)
                self.assertTrue(stopping.is_set())

        self.assertEqual(f"previous-{signal.SIGTERM}", handlers[signal.SIGTERM])
        self.assertEqual(f"previous-{signal.SIGINT}", handlers[signal.SIGINT])


if __name__ == "__main__":
    unittest.main()
