"""Allow running cmlxc via ``python -m cmlxc``."""

import sys

from cmlxc.cli import main

sys.exit(main() or 0)
