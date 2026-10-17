import os
import sys
from pathlib import Path

# Run from anywhere: make the repo root importable (agent/, ai/, ...).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Qt widgets in tests render offscreen — nothing ever appears on screen.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
