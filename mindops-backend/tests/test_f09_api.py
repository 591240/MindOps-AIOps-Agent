"""F-09 接口验收（文档 V1.1 §9.2）。

所有 async DB 操作在子进程里完成，TestClient 内部只做同步 HTTP。
"""

import json
import os
import re
import subprocess
import sys
import uuid

os.environ.setdefault("MINDOPS_MYSQL_DB", "mindops_f09_api_test")

# _f09_seed.py 在独立子进程里执行，且对该变量做了空值保护：
# 若这里已设成非空，子进程会继承同一把主密钥，跨进程解密才能成功。
# 因此必须用"直接赋值"而非 setdefault（外层 conftest / 其他模块可能已把空串写进去，
# 例如 _f09_seed.py 自己的 setdefault("", ...) 语义）。
os.environ["MINDOPS_LLM_VAULT_KEY"] = __import__(
    "cryptography.fernet", fromlist=["Fernet"]
).Fernet.generate_key().decode()

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

# 主密钥与库名的落地由 conftest.py 的 `_per_module_test_db` fixture 统一处理
# （它会在本模块用例开始前重建 settings 单例并同步所有 app.* 模块的引用）。
# 这里不再自行操作 settings —— 自行 cache_clear 会造出"第三个" settings 对象，
# 让其他模块拿到过期库名。

base = "/api/v1/llm"
_SEED = os.path.join(os.path.dirname(__file__), "_f09_seed.py")
_REF = os.path.join(os.path.dirname(__file__), "_f09_ref.py")


def _run_seed():
    subprocess.run([sys.executable, _SEED], timeout=30, check=True)


def _get_ref_id():
    r = subprocess.run([sys.executable, _REF], capture_output=True, text=True, timeout=30)
    return int(r.stdout.strip())


def _login_token(c, username="sre_admin"):
    # sre_admin 由 app.main._seed_users 建（密码 Admin@123，不是用户名本身）；
    # eng_test 由 _f09_seed.py 建（密码与用户名相同）。
    #
    # 注意：本函数**不得**修改 os.environ["MINDOPS_MYSQL_DB"]。
    # init_db 在 TestClient 进入时已按模块顶部的 setdefault 绑定到
    # mindops_f09_api_test，此处再改环境变量不会切换连接，只会污染
    # 后续模块（如 test_f11.py）的库名解析。
    pwd = "eng_test" if username == "eng_test" else "Admin@123"
    r = c.post("/api/v1/auth/login", json={"username": username, "password": pwd})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _scan(text):
    return re.findall(r"sk-[A-Za-z0-9]{8,}", text)


def test_f09_full_api_flow(monkeypatch):
    _run_seed()
    ref_id = _get_ref_id()

    async def fake_post(*_, **__):
        return {"model": "deepseek-chat", "choices": [{"message": {"content": "pong"}}], "usage": ""}
    monkeypatch.setattr("app.llm.gateway._post_chat", fake_post)

    with TestClient(app) as c:
        ah = _login_token(c, "sre_admin")
        eh = _login_token(c, "eng_test")

        # TC-2 保存即测试成功
        key = f"sk-test-{uuid.uuid4().hex[:12]}"
        r = c.post(f"{base}/providers", headers=ah, json={
            "name": "test-ds", "provider_type": "deepseek",
            "base_url": "https://api.deepseek.com/v1",
            "model_name": "deepseek-chat", "api_key": key,
            "role": "chat", "is_default": True,
        })
        assert r.status_code == 201, r.text
        assert r.json()["test"]["status"] == "success"
        assert _scan(json.dumps(r.json())) == []
        ds_id = r.json()["id"]

        # TC-3 保存即测试失败
        async def fake_fail(*_, **__):
            from app.llm.gateway import LlmCallError
            raise LlmCallError("key_invalid", "HTTP 401")
        monkeypatch.setattr("app.llm.gateway._post_chat", fake_fail)
        r = c.post(f"{base}/providers", headers=ah, json={
            "name": "test-bad", "provider_type": "deepseek",
            "base_url": "https://api.deepseek.com/v1",
            "model_name": "deepseek-chat", "api_key": "sk-badkey", "role": "chat",
        })
        assert r.status_code == 400
        assert r.json()["detail"]["reason"] == "key_invalid"

        # TC-4 embedding 拦截
        monkeypatch.setattr("app.llm.gateway._post_chat", fake_post)
        r = c.post(f"{base}/providers", headers=ah, json={
            "name": "test-emb", "provider_type": "qwen",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "model_name": "text-embedding-v4", "api_key": "sk-xxx", "role": "embedding",
        })
        assert r.status_code == 400
        assert "1024" in r.json()["detail"]["message"]

        # TC-5 默认互斥
        r = c.post(f"{base}/providers", headers=ah, json={
            "name": "test-ds2", "provider_type": "deepseek",
            "base_url": "https://api.deepseek.com/v1",
            "model_name": "deepseek-chat", "api_key": "sk-xxx",
            "role": "chat", "is_default": True,
        })
        assert r.status_code == 201
        old = next(p for p in c.get(f"{base}/providers", headers=ah).json()["providers"]
                   if p["name"] == "test-ds")
        assert old["is_default"] is False

        # TC-16 presets 权限
        assert c.get(f"{base}/presets", headers=eh).status_code == 403
        r = c.get(f"{base}/presets", headers=ah)
        assert r.status_code == 200 and len(r.json()["presets"]) == 7

        # TC-12 权限边界
        assert c.post(f"{base}/providers", headers=eh, json={
            "name": "x", "provider_type": "deepseek",
            "base_url": "https://api.deepseek.com/v1",
            "model_name": "deepseek-chat", "api_key": "sk-xxx", "role": "chat",
        }).status_code == 403
        assert c.get(f"{base}/providers", headers=eh).status_code == 200

        # PUT /test
        assert c.post(f"{base}/providers/{ds_id}/test", headers=ah).status_code == 200
        assert c.put(f"{base}/providers/{ds_id}", headers=ah,
                     json={"is_default": True}).status_code == 200

        # TC-11 软删除保护
        r = c.delete(f"{base}/providers/{ref_id}", headers=ah)
        assert r.status_code == 200
        assert r.json()["deleted"] is False and r.json()["soft"] is True

        # 物理删除
        r = c.delete(f"{base}/providers/{ds_id}", headers=ah)
        assert r.status_code == 200 and r.json()["deleted"] is True
