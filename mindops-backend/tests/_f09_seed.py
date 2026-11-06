"""F-09 API 测试种子脚本（子进程运行）。"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("MINDOPS_MYSQL_DB", "mindops_f09_api_test")
if "MINDOPS_LLM_VAULT_KEY" not in os.environ:
    os.environ["MINDOPS_LLM_VAULT_KEY"] = ""


async def seed():
    from app.db import init_db
    from app.auth.password import hash_password
    from app.models import IncidentStep, LlmProvider, User
    await init_db()
    await LlmProvider.filter(name__startswith="test-").delete()
    await User.filter(username="eng_test").delete()
    await User.create(username="eng_test", hashed_password=hash_password("eng_test"), role="engineer")
    ref = await LlmProvider.create(name="test-referenced", role="chat", provider_type="system")
    await IncidentStep.create(
        incident_id=99999, run_id="test-run", step="diagnose",
        agent="DiagnoseAgent", input_summary="{}", output_summary="{}",
        latency_ms=0, model_id=ref.id,
    )


if __name__ == "__main__":
    asyncio.run(seed())
