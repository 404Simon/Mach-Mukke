import os
import secrets
from pathlib import Path

WISHES_DIR = Path("wishes")
DOWNLOADS_DIR = Path("downloads")
TMP_DIR = Path("downloads_tmp")
API_KEY = os.environ.get("MACH_MUKKE_API_KEY", secrets.token_hex(32))
MAX_DURATION_SECONDS = 10 * 60
LASTFM_API_KEY = os.environ.get("MACH_MUKKE_LASTFM_API_KEY", "")
LASTFM_API_SECRET = os.environ.get("MACH_MUKKE_LASTFM_API_SECRET", "")
LASTFM_API_URL = "https://ws.audioscrobbler.com/2.0/"

BIRTHDAY_NAME = os.environ.get("BIRTHDAY_NAME", "Mustermann")
BIRTHDAY_AGE = os.environ.get("BIRTHDAY_AGE", "999")
COOKIE_SECRET = os.environ.get("COOKIE_SECRET", secrets.token_hex(32))
