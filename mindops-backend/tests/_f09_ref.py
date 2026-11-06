"""F-09 API 测试：获取 test-referenced 的 id。"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("MINDOPS_MYSQL_DB", "mindops_f09_api_test")


async def main():
    from app.db import init_db
    from app.models import LlmProvider
    await init_db()
    p = await LlmProvider.get(name="test-referenced")
    print(p.id)


if __name__ == "__main__":
    asyncio.run(main())
