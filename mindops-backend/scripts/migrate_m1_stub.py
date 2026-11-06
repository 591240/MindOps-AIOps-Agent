"""执行 F-02 §6.5 的 M1 聚类桩存量迁移。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# Allow `python scripts/migrate_m1_stub.py` from the backend project root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import close_db, init_db
from app.incidents.migration import migrate_m1_stub_incidents


async def main() -> int:
    await init_db()
    try:
        report = await migrate_m1_stub_incidents()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report["without_component"] or report["open_fingerprint_duplicates"] else 0
    finally:
        await close_db()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
