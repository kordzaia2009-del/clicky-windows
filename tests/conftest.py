import sys
from pathlib import Path

# Run from anywhere: make the repo root importable (agent/, ai/, ...).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
