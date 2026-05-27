"""Add src/ to sys.path so tests can `from shared import ...`."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
