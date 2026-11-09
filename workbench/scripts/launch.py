"""Start source checkout without an editable install."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(1, str(Path(__file__).resolve().parents[1]))
from autotriager.server import main
if __name__ == "__main__":
    main()
