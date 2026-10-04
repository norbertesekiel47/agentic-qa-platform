import sys

from aqa_core.project import SecretDestination
from aqa_runner.bound_secrets import BoundSecret
from aqa_runner.redaction import Redactor
from pydantic import SecretStr

CASES = {
    "ascii": [
        ("fake-abcdefghijklmnopqrstuvwx0", "fake-abcdefghijklmnopqrstuvwx1"),
        ("fake-" + "a" * 80 + "0", "fake-" + "a" * 80 + "1"),
    ],
    "optional": [
        ("fake-" + "\u200b" * 28 + "end", "fake-" + "\u200b" * 14 + "X"),
        ("fake-" + "\u00ad" * 32 + "end", "fake-" + "\u00ad" * 16 + "X"),
    ],
    "spaces": [
        ("fake" + (" " + "\u200b") * 12 + " end", "fake" + " " * 24 + "X"),
        ("fake" + (" " + "\u00ad") * 24 + " end", "fake" + " " * 48 + "X"),
    ],
}


def exercise(case: str) -> None:
    for value, near_match in CASES[case]:
        bound = BoundSecret(
            "FAKE",
            SecretStr(value),
            SecretDestination(("https://fake.test",), "password"),
        )
        redactor = Redactor([bound])
        assert redactor.redact(near_match) == near_match
        assert redactor.redact(value) == "[SECRET:FAKE]"
    sys.stdout.write(f"{case}: near-matches unchanged; matches redacted\n")


if __name__ == "__main__":
    exercise(sys.argv[1])
