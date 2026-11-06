"""F-11 集成管理测试（27-F-11 §9.1 验收矩阵 TC-1~TC-20）。

自包含：不依赖外部 mock，测试内自起临时 Prometheus（随机端口）。

单独运行：
    pytest tests/test_f11.py -p no:cacheprovider
"""

import asyncio
import contextlib
import json
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("MINDOPS_MYSQL_DB", "mindops_f11_test")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.integrations.probes import probe_prometheus  # noqa: E402
from app.integrations.ssrf_guard import SsrfBlocked, validate_base_url  # noqa: E402

ADMIN = ("sre_admin", "Admin@123")
ENGINEER = ("sre001", "P@ssw0rd!")


# ============ 基础设施 ============

def _setup():
    """在子进程里初始化 DB（彻底隔离事件循环，与 test_f10 同策略）。

    注意：子进程会**继承**父进程环境。若前面的模块（如 test_f09_api.py）
    设置过 MINDOPS_LLM_VAULT_KEY，此处再执行 setdefault("", ...) 就等于
    把已有密钥覆盖成空串，导致"跨模块串跑"时本套用例在 vault 相关断言上失败。
    因此这里显式清掉该变量，保证 F-11 子进程处于"未配置主密钥"的既定状态。
    """
    import subprocess

    child_env = {**os.environ, "MINDOPS_MYSQL_DB": "mindops_f11_test"}
    child_env.pop("MINDOPS_LLM_VAULT_KEY", None)

    subprocess.run(
        [sys.executable, "-c", """
import asyncio
asyncio.run(__import__('app.db', fromlist=['init_db']).init_db())
"""],
        timeout=60,
        check=True,
        env=child_env,
    )


def _purge():
    """清空 integrations 表（子进程内执行，隔离事件循环）。

    必须清场：测试大量使用固定地址（19123/19126/19300…），
    残留数据会触发 uk_base_scrape 唯一键冲突，导致重跑必挂。
    """
    import subprocess

    child_env = {**os.environ, "MINDOPS_MYSQL_DB": "mindops_f11_test"}
    child_env.pop("MINDOPS_LLM_VAULT_KEY", None)

    subprocess.run(
        [sys.executable, "-c", """
import asyncio, os
from tortoise import Tortoise
from app.config import settings

async def m():
    await Tortoise.init(config={
        "connections": {"default": settings.database_url},
        "apps": {"models": {"models": ["app.models"], "default_connection": "default"}},
        "use_tz": False, "timezone": "Asia/Shanghai",
    })
    from app.models import Integration
    await Integration.all().delete()
    await Tortoise.close_connections()

asyncio.run(m())
"""],
        timeout=60,
        check=True,
        env=child_env,
    )


@pytest.fixture(scope="session", autouse=True)
def _prepare_db():
    _setup()
    _purge()


class MockProm:
    """临时 Prometheus mock（随机端口）。

    mode: ok | redirect(302) | http404 | badjson
    requests: [(method, path), ...] 用于 TC-11 只读断言
    stop(): 提前关闭（用于制造"目标变为不可达"）
    """

    def __init__(self, up_count: int = 11, total: int = 12, mode: str = "ok") -> None:
        self.requests: list[tuple[str, str]] = []

        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _send(self, code, payload=None, headers=None, raw=None):
                body = raw if raw is not None else (
                    b"" if payload is None else json.dumps(payload).encode()
                )
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                outer.requests.append(("GET", self.path))
                if mode == "redirect":
                    self._send(302, headers={"Location": "http://8.8.8.8/"})
                elif mode == "http404":
                    self._send(404, {"status": "error"})
                elif mode == "badjson":
                    self._send(200, raw=b"<html>not json</html>")
                elif self.path in ("/api/v1/status", "/api/v1/status/runtimeinfo"):
                    self._send(200, {"status": "success", "data": {"version": "2.53.0"}})
                elif self.path == "/api/v1/targets":
                    self._send(200, {"status": "success", "data": {"activeTargets": [
                        {"health": "up" if i <= up_count else "down"}
                        for i in range(1, total + 1)
                    ]}})
                else:
                    self._send(404, {"status": "error"})

            def do_POST(self):  # noqa: N802
                outer.requests.append(("POST", self.path))
                self._send(405, {"status": "error"})

            def log_message(self, *args):  # 静音
                pass

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self._srv.server_address[1]}"
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()

    def __enter__(self) -> "MockProm":
        return self

    def __exit__(self, *exc) -> None:
        self.stop()


def _client():
    from app.main import app
    return TestClient(app)


def _login(c, cred=ADMIN) -> str:
    r = c.post("/api/v1/auth/login", json={"username": cred[0], "password": cred[1]})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create(c, token, base_url, scrape="", name="t") -> int:
    r = c.post("/api/v1/integrations", headers=_h(token), json={
        "name": name, "type": "prometheus", "base_url": base_url, "scrape_config": scrape,
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ============ 纯逻辑：SSRF 五道闸（TC-1/3/4/5/6） ============

def test_tc1_public_ip_rejected():
    with pytest.raises(SsrfBlocked):
        validate_base_url("http://8.8.8.8:9090")


def test_tc3_metadata_endpoint_rejected():
    with pytest.raises(SsrfBlocked):
        validate_base_url("http://169.254.169.254/")


@pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://x", "ftp://10.0.0.5"])
def test_tc4_bad_scheme_rejected(url):
    with pytest.raises(SsrfBlocked):
        validate_base_url(url)


def test_tc5_userinfo_rejected():
    with pytest.raises(SsrfBlocked):
        validate_base_url("http://evil@10.0.0.5:9090")


def test_tc6_multi_a_record_partial_escape(monkeypatch):
    """任一解析 IP 越界即拒（防 DNS 负载均衡把请求带到公网）。"""
    def fake_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(SsrfBlocked):
        validate_base_url("http://mixed.example.com:9090")


def test_internal_ok_and_normalized():
    assert validate_base_url("http://10.0.0.5:9090/") == "http://10.0.0.5:9090"
    assert validate_base_url("http://127.0.0.1:19090") == "http://127.0.0.1:19090"


def test_dns_failure_rejected(monkeypatch):
    def boom(*a, **kw):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    with pytest.raises(SsrfBlocked):
        validate_base_url("http://nonexistent.invalid:9090")


# ============ 纯逻辑：探测行为（TC-10/11 + 降级分支） ============

def test_tc7_probe_success_counts_targets():
    with MockProm(up_count=11, total=12) as mp:
        res = asyncio.run(probe_prometheus(mp.base_url))
    assert res.ok is True
    assert res.targets_total == 12 and res.targets_up == 11
    assert res.latency_ms >= 0


def test_tc10_probe_does_not_follow_redirect():
    with MockProm(mode="redirect") as mp:
        res = asyncio.run(probe_prometheus(mp.base_url))
    assert res.ok is False
    assert res.reason == "http_error"      # 302 不被跟随，直接判非 200


def test_tc11_probe_is_readonly_and_path_whitelisted():
    with MockProm() as mp:
        asyncio.run(probe_prometheus(mp.base_url))
    assert len(mp.requests) == 2, mp.requests
    assert all(m == "GET" for m, _ in mp.requests), mp.requests
    assert {p for _, p in mp.requests} == {"/api/v1/status/runtimeinfo", "/api/v1/targets"}


def test_probe_http404_is_http_error():
    with MockProm(mode="http404") as mp:
        res = asyncio.run(probe_prometheus(mp.base_url))
    assert res.ok is False and res.reason == "http_error"


def test_probe_non_json_is_bad_response():
    with MockProm(mode="badjson") as mp:
        res = asyncio.run(probe_prometheus(mp.base_url))
    assert res.ok is False and res.reason == "bad_response"


def test_probe_connection_refused():
    # 超时给足 8s：部分 Windows 环境对关闭端口约 2s 才回 RST，
    # 若超时设得太短会被误判为 timeout（两者都是 §3.3 定义的"连不上"分类）。
    res = asyncio.run(probe_prometheus("http://127.0.0.1:19123", timeout_ms=8000))
    assert res.ok is False
    assert res.reason in {"connection_refused", "timeout"}, res
    assert res.latency_ms == 0


def test_probe_error_string_truncated_to_255():
    res = asyncio.run(probe_prometheus("http://127.0.0.1:19123", timeout_ms=8000))
    assert res.ok is False
    assert len(res.error) <= 255


# ============ 接口层 ============

# ============ API 组：TestClient 进入前先清场 ============
#
# F-11 的 API 用例大量复用固定端口与固定 scrape 选择器，而
# `integrations` 表有唯一键 `uk_base_scrape`。若库里残留上一次运行
# （或其他测试模块）留下的同名记录，创建接口会返回 409 DUPLICATE_TARGET，
# 表现为一串看不懂的断言失败。session fixture 只在会话开始时 clear 一次，
# 跨模块串跑时不足以兜底，因此这里在 API 组入口再清一次。
_purge()


def test_tc2_create_internal_returns_pending():
    with MockProm() as mp, _client() as c:
        token = _login(c)
        r = c.post("/api/v1/integrations", headers=_h(token), json={
            "name": "订单服务集群", "type": "prometheus",
            "base_url": mp.base_url, "scrape_config": 'job="tc2"',
        })
        assert r.status_code == 201, r.text
        assert r.json()["status"] == "pending"


def test_tc1_api_public_url_not_persisted():
    with _client() as c:
        token = _login(c)
        r = c.post("/api/v1/integrations", headers=_h(token), json={
            "name": "坏目标", "type": "prometheus", "base_url": "http://8.8.8.8:9090",
        })
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "INVALID_BASE_URL"
        lst = c.get("/api/v1/integrations", headers=_h(token)).json()["integrations"]
        assert not any(i["base_url"] == "http://8.8.8.8:9090" for i in lst)


def test_tc20_invalid_type_rejected():
    with _client() as c:
        token = _login(c)
        r = c.post("/api/v1/integrations", headers=_h(token), json={
            "name": "gf", "type": "grafana", "base_url": "http://10.0.0.5:3000",
        })
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "INVALID_TYPE"


def test_tc17_duplicate_target_rejected():
    with MockProm() as mp, _client() as c:
        token = _login(c)
        payload = {"name": "dup", "type": "prometheus",
                   "base_url": mp.base_url, "scrape_config": 'job="dup"'}
        assert c.post("/api/v1/integrations", headers=_h(token), json=payload).status_code == 201
        r = c.post("/api/v1/integrations", headers=_h(token), json=payload)
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "DUPLICATE_TARGET"


def test_tc7_api_probe_activates_and_updates_fields():
    with MockProm(up_count=11, total=12) as mp, _client() as c:
        token = _login(c)
        iid = _create(c, token, mp.base_url, 'job="tc7"')
        r = c.post(f"/api/v1/integrations/{iid}/test", headers=_h(token))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True and body["status"] == "active"
        assert body["targets_total"] == 12 and body["targets_up"] == 11
        assert body["last_heartbeat"]

        row = next(i for i in c.get("/api/v1/integrations", headers=_h(token))
                   .json()["integrations"] if i["id"] == iid)
        assert row["status"] == "active" and row["last_error"] is None
        assert row["targets_total"] == 12


def test_tc8_api_probe_failure_sets_error():
    with _client() as c:
        token = _login(c)
        iid = _create(c, token, "http://127.0.0.1:19123", 'job="tc8"')
        r = c.post(f"/api/v1/integrations/{iid}/test", headers=_h(token))
        assert r.status_code == 200                      # 探测失败是业务结果，非 4xx
        body = r.json()
        assert body["ok"] is False and body["status"] == "error"
        assert body["reason"] == "connection_refused"
        assert body["last_error"]
        assert body["last_heartbeat"] is None


def test_tc9_heartbeat_preserved_across_failure():
    """FR-6：失败只写 last_error，保留上次成功心跳。"""
    with MockProm() as mp, _client() as c:
        token = _login(c)
        iid = _create(c, token, mp.base_url, 'job="tc9"')

        ok_body = c.post(f"/api/v1/integrations/{iid}/test", headers=_h(token)).json()
        assert ok_body["ok"] is True
        h1 = ok_body["last_heartbeat"]

        mp.stop()   # 目标变为不可达

        fail_body = c.post(f"/api/v1/integrations/{iid}/test", headers=_h(token)).json()
        assert fail_body["ok"] is False
        assert fail_body["last_heartbeat"] == h1, "失败不得清空上次成功心跳"


def test_tc13_cannot_set_pending_or_error_manually():
    with MockProm() as mp, _client() as c:
        token = _login(c)
        iid = _create(c, token, mp.base_url, 'job="tc13"')
        for bad in ("pending", "error"):
            r = c.put(f"/api/v1/integrations/{iid}", headers=_h(token), json={"status": bad})
            assert r.status_code == 422, f"{bad} 应被拒"


def test_tc13b_pending_and_error_targets_can_be_disabled():
    """停用是安全动作：未探测（pending）与异常（error）目标都必须能被停用。"""
    with _client() as c:
        token = _login(c)
        # pending → disabled
        iid = _create(c, token, "http://127.0.0.1:19123", 'job="tc13b-pending"')
        r = c.put(f"/api/v1/integrations/{iid}", headers=_h(token), json={"status": "disabled"})
        assert r.status_code == 200 and r.json()["status"] == "disabled", r.text

        # error → disabled
        iid2 = _create(c, token, "http://127.0.0.1:19124", 'job="tc13b-error"')
        c.post(f"/api/v1/integrations/{iid2}/test", headers=_h(token))       # → error
        r2 = c.put(f"/api/v1/integrations/{iid2}", headers=_h(token), json={"status": "disabled"})
        assert r2.status_code == 200 and r2.json()["status"] == "disabled", r2.text


def test_tc13c_pending_cannot_jump_to_active():
    """未经探测直接标记"已连通"就是状态撒谎。"""
    with _client() as c:
        token = _login(c)
        iid = _create(c, token, "http://127.0.0.1:19125", 'job="tc13c"')
        r = c.put(f"/api/v1/integrations/{iid}", headers=_h(token), json={"status": "active"})
        assert r.status_code == 422, r.text


def test_tc12_disabled_target_cannot_be_probed():
    with MockProm() as mp, _client() as c:
        token = _login(c)
        iid = _create(c, token, mp.base_url, 'job="tc12"')
        c.put(f"/api/v1/integrations/{iid}", headers=_h(token), json={"status": "disabled"})
        r = c.post(f"/api/v1/integrations/{iid}/test", headers=_h(token))
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "TARGET_DISABLED"


def test_tc14_change_base_url_resets_to_pending():
    with MockProm() as mp, _client() as c:
        token = _login(c)
        iid = _create(c, token, mp.base_url, 'job="tc14"')
        c.post(f"/api/v1/integrations/{iid}/test", headers=_h(token))     # → active
        r = c.put(f"/api/v1/integrations/{iid}", headers=_h(token),
                  json={"base_url": "http://127.0.0.1:19099"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "pending"
        assert body["last_heartbeat"] is None
        assert body["targets_total"] is None


def test_tc15_soft_delete_is_idempotent():
    with _client() as c:
        token = _login(c)
        iid = _create(c, token, "http://127.0.0.1:19126", 'job="tc15"')
        r1 = c.delete(f"/api/v1/integrations/{iid}", headers=_h(token))
        assert r1.status_code == 200
        body = r1.json()
        assert body["soft"] is True and body["deleted"] is False
        assert "审计" in body["reason"]

        r2 = c.delete(f"/api/v1/integrations/{iid}", headers=_h(token))
        assert r2.status_code == 200 and r2.json() == body

        rows = c.get("/api/v1/integrations?include_disabled=true",
                     headers=_h(token)).json()["integrations"]
        row = next(i for i in rows if i["id"] == iid)
        assert row["status"] == "disabled" and row["disabled_at"]


def test_tc16_list_filters_disabled_by_default():
    with _client() as c:
        token = _login(c)
        iid = _create(c, token, "http://127.0.0.1:19127", 'job="tc16"')
        c.delete(f"/api/v1/integrations/{iid}", headers=_h(token))

        default_ids = [i["id"] for i in
                       c.get("/api/v1/integrations", headers=_h(token)).json()["integrations"]]
        assert iid not in default_ids
        all_ids = [i["id"] for i in c.get("/api/v1/integrations?include_disabled=true",
                                          headers=_h(token)).json()["integrations"]]
        assert iid in all_ids


def test_tc18_permission_boundary():
    with MockProm() as mp, _client() as c:
        admin, eng = _login(c, ADMIN), _login(c, ENGINEER)
        iid = _create(c, admin, mp.base_url, 'job="tc18"')

        assert c.get("/api/v1/integrations", headers=_h(eng)).status_code == 200
        assert c.post("/api/v1/integrations", headers=_h(eng), json={
            "name": "x", "type": "prometheus", "base_url": "http://10.0.0.9:9090",
        }).status_code == 403
        assert c.put(f"/api/v1/integrations/{iid}", headers=_h(eng),
                     json={"name": "hack"}).status_code == 403
        assert c.delete(f"/api/v1/integrations/{iid}", headers=_h(eng)).status_code == 403
        assert c.post(f"/api/v1/integrations/{iid}/test", headers=_h(eng)).status_code == 403

        assert c.get("/api/v1/integrations").status_code == 401


def test_tc19_probe_rate_limited_after_5():
    with _client() as c:
        token = _login(c)
        iid = _create(c, token, "http://127.0.0.1:19128", 'job="tc19"')
        codes = [c.post(f"/api/v1/integrations/{iid}/test", headers=_h(token)).status_code
                 for _ in range(6)]
        assert codes[:5] == [200] * 5, codes
        assert codes[5] == 429, codes
        assert c.post(f"/api/v1/integrations/{iid}/test",
                      headers=_h(token)).json()["detail"]["code"] == "RATE_LIMITED"


def test_create_rate_limited_after_10():
    """POST 按 user 10 次/60s 限流，防把创建接口当内网扫描器。"""
    with _client() as c:
        token = _login(c)
        codes = []
        for n in range(11):
            codes.append(c.post("/api/v1/integrations", headers=_h(token), json={
                "name": f"scan{n}", "type": "prometheus",
                "base_url": f"http://127.0.0.1:{19300 + n}",
            }).status_code)
        assert codes[:10] == [201] * 10, codes
        assert codes[10] == 429, codes
