"""Print the API's OpenAPI schema; frontend `npm run gen:api` turns it into TS types."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server import create_app  # noqa: E402

json.dump(create_app(engine=None).openapi(), sys.stdout, ensure_ascii=False, indent=2)
