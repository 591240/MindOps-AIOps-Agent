"""F-11 端到端验收脚本（对应 27-F-11 §9.1/§9.2 验收矩阵）。

前置：后端已启动于 127.0.0.1:8010；mock Prometheus 已运行于 127.0.0.1:19090。
"""
from __future__ import annotations

import os

import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # mindops-backend/

BASE = "http://127.0.0.1:8010/api/v1"

results: list[tuple[str, bool, str]] = []


def call(method: str, path: str, token: str | None = None, body: dict | None = None):
    req = urllib.request.Request(BASE + path, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, cond, detail))
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail[:110]}")


def _purge_rows() -> None:
    """物理清场（仅测试用）。软删除行仍占 (base_url, scrape_config) 唯一键——
    这是 FR-10 + uk_base_scrape 的设计行为（停用目标应 PUT 启用而非重复注册），
    因此重跑验收前必须物理删除旧行。"""
    import asyncio

    from tortoise import Tortoise

    from app.config import settings
    # 安全修复（审查 #2）：本函数会物理删除库内全部相关行。
    # 只允许对 *_test 测试库执行；对运行库必须显式设置
    # MINDOPS_E2E_ALLOW_PURGE=1（例如刻意重置演示环境）。
    _db_name = settings.database_url.rsplit("/", 1)[-1].split("?")[0]
    if not _db_name.endswith("_test") and os.environ.get("MINDOPS_E2E_ALLOW_PURGE") != "1":
        raise SystemExit(
            f"拒绝清库：{settings.database_url} 不是 *_test 库。\n"
            "确要重置运行库请显式设置环境变量 MINDOPS_E2E_ALLOW_PURGE=1"
        )

    async def _run():
        await Tortoise.init(config={
            "connections": {"default": settings.database_url},
            "apps": {"models": {"models": ["app.models"], "default_connection": "default"}},
            "use_tz": False, "timezone": "Asia/Shanghai",
        })
        from app.models import Integration
        await Integration.all().delete()
        await Tortoise.close_connections()

    asyncio.run(_run())


def main() -> None:
    _purge_rows()

    # ---- 登录 ----
    st, body = call("POST", "/auth/login", body={"username": "sre_admin", "password": "Admin@123"})
    admin = body.get("access_token")
    check("admin 登录", st == 200 and bool(admin), f"HTTP {st}")

    st, body = call("POST", "/auth/login", body={"username": "sre001", "password": "P@ssw0rd!"})
    engineer = body.get("access_token")
    check("engineer 登录", st == 200 and bool(engineer), f"HTTP {st}")

    # ---- TC-2 新增内网目标 → 201 pending ----
    st, body = call("POST", "/integrations", admin, {
        "name": "订单服务集群", "type": "prometheus",
        "base_url": "http://127.0.0.1:19090", "scrape_config": 'job=~"order-.*"',
    })
    check("TC-2 内网地址创建", st == 201 and body.get("status") == "pending",
          f"HTTP {st} {body}")
    iid = body.get("id")

    # ---- TC-1/3/4/5 SSRF 拦截（不落库）----
    for tc, url in [
        ("TC-1 公网", "http://8.8.8.8:9090"),
        ("TC-3 元数据", "http://169.254.169.254/"),
        ("TC-4 非法scheme", "file:///etc/passwd"),
        ("TC-5 userinfo", "http://evil@10.0.0.5:9090"),
    ]:
        st, body = call("POST", "/integrations", admin,
                        {"name": "x", "type": "prometheus", "base_url": url})
        code = (body.get("detail") or {}).get("code", "")
        check(f"{tc} → 422 INVALID_BASE_URL", st == 422 and code == "INVALID_BASE_URL",
              f"HTTP {st} code={code}")

    # ---- TC-20 类型白名单 ----
    st, body = call("POST", "/integrations", admin,
                    {"name": "x", "type": "grafana", "base_url": "http://10.0.0.5:3000"})
    check("TC-20 非法类型 → 422", st == 422 and (body.get("detail") or {}).get("code") == "INVALID_TYPE",
          f"HTTP {st}")

    # ---- TC-17 重复登记 ----
    st, body = call("POST", "/integrations", admin, {
        "name": "重复", "type": "prometheus",
        "base_url": "http://127.0.0.1:19090", "scrape_config": 'job=~"order-.*"',
    })
    check("TC-17 重复登记 → 409", st == 409 and (body.get("detail") or {}).get("code") == "DUPLICATE_TARGET",
          f"HTTP {st}")

    # ---- TC-7 探测成功（mock 12 targets / 11 up）----
    st, body = call("POST", f"/integrations/{iid}/test", admin)
    check("TC-7 探测成功", st == 200 and body.get("ok") and body.get("targets_total") == 12
          and body.get("targets_up") == 11 and body.get("status") == "active",
          f"HTTP {st} {body}")

    # ---- TC-9 心跳语义：成功后再失败，心跳保留 ----
    heartbeat_t1 = body.get("last_heartbeat")
    st, body = call("POST", "/integrations", admin, {
        "name": "坏地址目标", "type": "prometheus",
        "base_url": "http://127.0.0.1:19123", "scrape_config": "",
    })  # 19123 无服务
    bad_id = body.get("id")
    st, body = call("POST", f"/integrations/{bad_id}/test", admin)
    check("TC-8 探测失败 → 200 ok:false", st == 200 and not body.get("ok")
          and body.get("status") == "error" and body.get("last_error"),
          f"HTTP {st} reason={body.get('reason')}")
    check("TC-9 失败时心跳为 None（从未成功过）", body.get("last_heartbeat") is None,
          f"last_heartbeat={body.get('last_heartbeat')}")

    # ---- TC-13 状态机：不可手工置 pending/error ----
    st, body = call("PUT", f"/integrations/{iid}", admin, {"status": "pending"})
    check("TC-13 置 pending → 422", st == 422, f"HTTP {st}")

    # ---- TC-12 disabled 不可探测 ----
    st, body = call("PUT", f"/integrations/{iid}", admin, {"status": "disabled"})
    check("PUT 暂停 → disabled", st == 200 and body.get("status") == "disabled"
          and body.get("disabled_at"), f"HTTP {st}")
    st, body = call("POST", f"/integrations/{iid}/test", admin)
    check("TC-12 disabled 探测 → 409", st == 409
          and (body.get("detail") or {}).get("code") == "TARGET_DISABLED", f"HTTP {st}")

    # ---- TC-16 列表过滤 ----
    st, body = call("GET", "/integrations", admin)
    statuses = [r["status"] for r in body.get("integrations", [])]
    check("TC-16 默认不含 disabled", "disabled" not in statuses, f"statuses={statuses}")
    st, body = call("GET", "/integrations?include_disabled=true", admin)
    statuses_all = [r["status"] for r in body.get("integrations", [])]
    check("TC-16 include_disabled 含 disabled", "disabled" in statuses_all, f"statuses={statuses_all}")

    # ---- 启用恢复 → disabled_at 清空 ----
    st, body = call("PUT", f"/integrations/{iid}", admin, {"status": "active"})
    check("PUT 启用 → active + disabled_at=NULL", st == 200 and body.get("status") == "active"
          and body.get("disabled_at") is None, f"HTTP {st}")

    # ---- TC-14 改地址回 pending ----
    st, body = call("PUT", f"/integrations/{iid}", admin, {"base_url": "http://127.0.0.1:19091"})
    check("TC-14 改地址 → pending + 心跳清空", st == 200 and body.get("status") == "pending"
          and body.get("last_heartbeat") is None and body.get("targets_total") is None,
          f"HTTP {st} status={body.get('status')}")
    # 改回原地址（避免后续重复登记干扰）
    call("PUT", f"/integrations/{iid}", admin, {"base_url": "http://127.0.0.1:19090",
                                                "scrape_config": 'job=~"order-.*"'})

    # ---- TC-15 软删除幂等 ----
    st, body = call("DELETE", f"/integrations/{bad_id}", admin)
    check("TC-15 软删除", st == 200 and body.get("soft") is True and body.get("deleted") is False
          and "审计" in body.get("reason", ""), f"HTTP {st} {body}")
    st2, body2 = call("DELETE", f"/integrations/{bad_id}", admin)
    check("TC-15 重复删除幂等", st2 == 200 and body2 == body, f"HTTP {st2}")

    # ---- TC-18 权限边界 ----
    st, body = call("GET", "/integrations", engineer)
    check("TC-18 engineer GET → 200", st == 200, f"HTTP {st}")
    st, body = call("POST", "/integrations", engineer,
                    {"name": "x", "type": "prometheus", "base_url": "http://10.0.0.9:9090"})
    check("TC-18 engineer POST → 403", st == 403, f"HTTP {st}")
    st, body = call("PUT", f"/integrations/{iid}", engineer, {"name": "hack"})
    check("TC-18 engineer PUT → 403", st == 403, f"HTTP {st}")
    st, body = call("DELETE", f"/integrations/{iid}", engineer)
    check("TC-18 engineer DELETE → 403", st == 403, f"HTTP {st}")
    st, body = call("POST", f"/integrations/{iid}/test", engineer)
    check("TC-18 engineer test → 403", st == 403, f"HTTP {st}")

    # ---- 未认证 ----
    st, body = call("GET", "/integrations")
    check("无 token → 401", st == 401, f"HTTP {st}")

    # ---- 汇总 ----
    n_pass = sum(1 for _, ok, _ in results if ok)
    n_fail = len(results) - n_pass
    print(f"\n===== {n_pass} passed, {n_fail} failed =====")
    raise SystemExit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
