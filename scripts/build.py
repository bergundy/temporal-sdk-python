import subprocess
import sys
from pathlib import Path

base_dir = Path(__file__).parent

if __name__ == "__main__":
    subprocess.check_call([sys.executable, str(base_dir / "gen-protos.py")])
    subprocess.check_call([sys.executable, str(base_dir / "build-bridge.py")])
