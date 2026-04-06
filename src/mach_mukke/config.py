import os
import secrets
from pathlib import Path

WISHES_DIR = Path("wishes")
DOWNLOADS_DIR = Path("downloads")
TMP_DIR = Path("downloads_tmp")
API_KEY = os.environ.get("MACH_MUKKE_API_KEY", secrets.token_hex(32))
MAX_DURATION_SECONDS = 10 * 60
