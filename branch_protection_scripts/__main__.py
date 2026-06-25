"""Allow running as: python -m branch_protection_scripts"""

from .cli import main
import sys

sys.exit(main())
