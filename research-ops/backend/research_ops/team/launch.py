import sys
from pathlib import Path

from .runtime import run_worker

if __name__ == "__main__":
    run_worker(Path(sys.argv[1]), sys.argv[2:] or None)
