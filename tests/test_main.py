"""Smoke test for the ``main()`` entry point."""

from pathlib import Path

import pytest

from lithos_lens.main import main


def test_main_prints_greeting(
    lithos_lens_config_env: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Silence unused-arg warning — the fixture sets LITHOS_LENS_CONFIG.
    assert lithos_lens_config_env.exists()

    main()

    captured = capsys.readouterr()
    assert captured.out.strip() == "Hello from Lithos Lens (test)"
