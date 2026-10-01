"""Tests for polling.py: waiting for a value to settle (ADR-0023).

Run: uv run python -m unittest discover -s bench/harness
"""

from __future__ import annotations

import unittest

import polling


class SettledTest(unittest.TestCase):
    def test_returns_the_value_once_it_stops_changing(self) -> None:
        reads: list[int] = []

        def read() -> int:
            reads.append(1)
            return 1 if len(reads) == 1 else 2

        self.assertEqual(polling.settled(read, quiet=0.04, timeout=2), 2)
        self.assertGreater(len(reads), 2)

    def test_fails_when_the_value_keeps_changing(self) -> None:
        reads: list[int] = []

        def read() -> int:
            reads.append(1)
            return len(reads)

        with self.assertRaisesRegex(
            polling.NotSettledError, r"still changing after 0\.2 s"
        ):
            polling.settled(read, quiet=0.1, timeout=0.2)


if __name__ == "__main__":
    unittest.main()
