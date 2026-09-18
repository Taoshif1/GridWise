import json
import sys
from pathlib import Path

import httpx


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    base_url = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:1337"

    health = httpx.get(f"{base_url}/health", timeout=10)
    health.raise_for_status()
    assert health.json() == {"status": "ok"}
    print("HEALTH OK:", health.json())

    payload = json.loads((ROOT / "sample_request.json").read_text(encoding="utf-8"))
    response = httpx.post(f"{base_url}/optimize-energy", json=payload, timeout=30)
    print("OPTIMIZE STATUS:", response.status_code)
    print(json.dumps(response.json(), indent=2))
    response.raise_for_status()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
