"""Hello-world entry point.

Loads configuration via :func:`lithos_lens.config.load_config` and prints a
greeting that shows the active environment.
"""

from __future__ import annotations

import sys

from lithos_lens.config import load_config
from lithos_lens.errors import LithosLensError


def main() -> None:
    """Print ``{greeting} from Lithos Lens ({environment})``.

    Exits with code 1 and a message on stderr when config cannot be
    loaded, so shell callers see a non-zero status.
    """
    try:
        config = load_config()
    except LithosLensError as exc:
        print(f"lithos-lens: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"{config.greeting} from Lithos Lens ({config.environment})")
