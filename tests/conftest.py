import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("BOT_TOKEN", "123456789:TEST_BOT_TOKEN_UNIT_TESTS")
os.environ.setdefault("DB_PATH", str(Path(tempfile.gettempdir()) / "media_saver_unit_test.sqlite3"))
