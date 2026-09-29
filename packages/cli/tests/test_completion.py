"""aqa offers no shell-completion options: API.md §7 lists its whole surface.

Only --show-completion is exercised, never --install-completion: if completion
came back, that option would rewrite the developer's shell startup file.
"""

import subprocess


def test_offers_no_shell_completion_options(aqa: str) -> None:
    result = subprocess.run(
        [aqa, "--show-completion"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 2, result.stdout
    assert "No such option" in result.stderr
