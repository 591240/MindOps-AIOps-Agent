"""F-12 端到端验收脚本（对应 30-F-12 §3 契约与 §9.1 验收矩阵）。

前置：后端已启动于 127.0.0.1:8010；mock Prometheus 已运行于 127.0.0.1:19090
      （python scripts/mock_prometheus.py 19090）。

用法：python scripts/e2e_f12.py
"""
from __future__ import annotations

import os

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE = "http://127.0.0.1:8010/api/v1"
PROM = "http://127.0.0.1:19090"

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
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail[:120]}")


def code_of(body: dict) -> str:
    return (body.get("detail") or {}).get("code", "")


def _purge() -> None:
    """物理清场（仅测试用）：清 tasks / task_targets / integrations / alerts。"""
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
        from app.models import Alert, Integration, Task, TaskTarget
        await TaskTarget.all().delete()
        await Task.all().delete()
        await Integration.all().delete()
        await Alert.all().delete()
        await Tortoise.close_connections()

    asyncio.run(_run())


def main() -> None:
    _purge()

    # ---- 登录 ----
    st, body = call("POST", "/auth/login", body={"username": "sre_admin", "password": "Admin@123"})
    admin = body.get("access_token")
    check("admin 登录", st == 200 and bool(admin), f"HTTP {st}")

    st, body = call("POST", "/auth/login", body={"username": "sre001", "password": "P@ssw0rd!"})
    eng = body.get("access_token")
    check("engineer 登录", st == 200 and bool(eng), f"HTTP {st}")

    # ---- 准备资产：一个 active 目标 + 一个 disabled 目标（F-11 侧）----
    st, body = call("POST", "/integrations", admin, {
        "name": "订单服务集群", "type": "prometheus",
        "base_url": PROM, "scrape_config": 'job="order"',
    })
    ig1 = body.get("id")
    call("POST", f"/integrations/{ig1}/test", admin)          # → active
    check("准备：目标 1 探测为 active", True, f"id={ig1}")

    st, body = call("POST", "/integrations", admin, {
        "name": "旧版统计服务", "type": "prometheus",
        "base_url": "http://127.0.0.1:19199", "scrape_config": 'job="legacy"',
    })
    ig2 = body.get("id")
    call("PUT", f"/integrations/{ig2}", admin, {"status": "disabled"})   # → disabled
    check("准备：目标 2 已停用", True, f"id={ig2}")

    # ---- TC-1 创建任务（携带 1 个目标）----
    st, body = call("POST", "/tasks", admin, {
        "name": "大促保障", "description": "双十一期间对订单链路的持续监控",
        "target_ids": [ig1],
    })
    check("TC-1 创建任务 → 201", st == 201 and body.get("target_count") == 1, f"HTTP {st} {body}")
    tid = body.get("id")

    # ---- TC-2 空任务允许（FR-6）----
    st, body = call("POST", "/tasks", admin, {"name": "空任务", "description": "", "target_ids": []})
    check("TC-2 空任务 → 201 且 count=0", st == 201 and body.get("target_count") == 0, f"HTTP {st}")

    # ---- TC-3 空名 / 纯空白名 → 422 EMPTY_NAME ----
    for label, nm in [("空串", ""), ("纯空白", "   ")]:
        st, body = call("POST", "/tasks", admin, {"name": nm, "target_ids": []})
        check(f"TC-3 {label}名 → 422 EMPTY_NAME", st == 422 and code_of(body) == "EMPTY_NAME",
              f"HTTP {st} code={code_of(body)}")

    # ---- TC-4 重名 → 409 ----
    st, body = call("POST", "/tasks", admin, {"name": "大促保障", "target_ids": []})
    check("TC-4 重名 → 409 DUPLICATE_TASK_NAME",
          st == 409 and code_of(body) == "DUPLICATE_TASK_NAME", f"HTTP {st}")

    # ---- TC-5 绑定停用目标 → 422 ----
    st, body = call("POST", "/tasks", admin, {"name": "带坏目标", "target_ids": [ig2]})
    check("TC-5 绑定停用目标 → 422 TARGET_DISABLED",
          st == 422 and code_of(body) == "TARGET_DISABLED", f"HTTP {st} {code_of(body)}")

    # ---- TC-6 绑定不存在目标 → 404 ----
    st, body = call("POST", "/tasks", admin, {"name": "带幻觉目标", "target_ids": [999999]})
    check("TC-6 绑定不存在目标 → 404 TARGET_NOT_FOUND",
          st == 404 and code_of(body) == "TARGET_NOT_FOUND", f"HTTP {st}")

    # ---- TC-7 列表：含 target_count / alerts_24h ----
    st, body = call("GET", "/tasks", admin)
    items = body.get("items", [])
    row = next((i for i in items if i["id"] == tid), None)
    check("TC-7 列表含任务且字段完整",
          st == 200 and row is not None and "target_count" in row and "alerts_24h" in row,
          f"HTTP {st} total={body.get('total')}")
    check("TC-7 默认不含 archived", all(i["status"] != "archived" for i in items),
          f"statuses={[i['status'] for i in items]}")

    # ---- TC-8 详情：targets 摘要 + 实时 target_count ----
    st, body = call("GET", f"/tasks/{tid}", admin)
    check("TC-8 详情含绑定项目摘要",
          st == 200 and body.get("target_count") == 1
          and body.get("targets") and body["targets"][0]["integration_id"] == ig1,
          f"HTTP {st} targets={len(body.get('targets', []))}")
    check("TC-8 详情回带目标连通状态",
          body["targets"][0].get("status") == "active", f"status={body['targets'][0].get('status')}")

    # ---- TC-9 不存在任务 → 404 ----
    st, body = call("GET", "/tasks/task_999", admin)
    check("TC-9 任务不存在 → 404", st == 404 and code_of(body) == "TASK_NOT_FOUND", f"HTTP {st}")

    # ---- TC-10 绑定 / 重复绑定 ----
    st, body = call("POST", f"/tasks/{tid}/targets", admin, {"integration_id": ig1})
    check("TC-10 重复绑定 → 409 ALREADY_BOUND",
          st == 409 and code_of(body) == "ALREADY_BOUND", f"HTTP {st}")

    # ---- TC-11 解绑 → target_count 递减 ----
    st, body = call("DELETE", f"/tasks/{tid}/targets/{ig1}", admin)
    check("TC-11 解绑 → count=0", st == 200 and body.get("target_count") == 0, f"HTTP {st} {body}")
    st, body = call("GET", f"/tasks/{tid}", admin)
    check("TC-11 详情 target_count 实时同步", body.get("target_count") == 0, f"count={body.get('target_count')}")

    # ---- TC-12 重复解绑 → 404 ----
    st, body = call("DELETE", f"/tasks/{tid}/targets/{ig1}", admin)
    check("TC-12 重复解绑 → 404 BINDING_NOT_FOUND",
          st == 404 and code_of(body) == "BINDING_NOT_FOUND", f"HTTP {st}")

    # ---- 重新绑定，供告警归属用 ----
    call("POST", f"/tasks/{tid}/targets", admin, {"integration_id": ig1})

    # ---- TC-13 状态流转 active ⇄ paused ----
    st, body = call("PUT", f"/tasks/{tid}", admin, {"status": "paused"})
    check("TC-13 active → paused", st == 200 and body.get("status") == "paused", f"HTTP {st}")
    st, body = call("PUT", f"/tasks/{tid}", admin, {"status": "active"})
    check("TC-13 paused → active", st == 200 and body.get("status") == "active", f"HTTP {st}")

    # ---- TC-14 非法状态 → 422 ----
    st, body = call("PUT", f"/tasks/{tid}", admin, {"status": "deleted"})
    check("TC-14 非法状态 → 422 INVALID_STATUS",
          st == 422 and code_of(body) == "INVALID_STATUS", f"HTTP {st} {code_of(body)}")

    # ---- TC-15 告警带上 integration_id → 归属到任务 ----
    st, body = call("POST", "/alerts/manual", admin, {
        "component": "order-svc", "severity": "P1", "title": "订单服务延迟升高",
        "detail": "p99 > 2s", "integration_id": ig1,
    })
    check("TC-15 注入带归属信息的告警", st == 200 and bool(body.get("alert_id")), f"HTTP {st}")

    # ---- TC-16 未归属告警（不带 integration_id）----
    st, body = call("POST", "/alerts/manual", admin, {
        "component": "redis", "severity": "P2", "title": "Redis 内存偏高", "detail": "used>80%",
    })
    check("TC-16 注入未归属告警", st == 200 and bool(body.get("alert_id")), f"HTTP {st}")

    # ---- TC-17 /alerts/recent?task_id=<id> 只返回本任务告警 ----
    st, body = call("GET", f"/alerts/recent?minutes=60&task_id={tid}", admin)
    items = body.get("items", [])
    check("TC-17 按任务筛选告警", st == 200 and len(items) >= 1
          and all(i["integration_id"] == ig1 for i in items), f"HTTP {st} n={len(items)}")
    check("TC-17 告警带归属任务名", any("大促保障" in i.get("task_names", []) for i in items),
          f"names={[i.get('task_names') for i in items]}")

    # ---- TC-18 task_id=none 返回未归属告警 ----
    st, body = call("GET", "/alerts/recent?minutes=60&task_id=none", admin)
    items = body.get("items", [])
    check("TC-18 未归属告警显式可见", st == 200 and any(i["integration_id"] is None for i in items),
          f"HTTP {st} n={len(items)}")

    # ---- TC-19 缺省行为不变（全部告警）----
    st, body = call("GET", "/alerts/recent?minutes=60", admin)
    check("TC-19 缺省返回全部（向后兼容）", st == 200 and len(body.get("items", [])) >= 2,
          f"HTTP {st} n={len(body.get('items', []))}")

    # ---- TC-23 N:N：同一目标同属两个任务 → 同一告警双向可见 ----
    st, body = call("POST", "/tasks", admin, {"name": "日常巡检", "target_ids": [ig1]})
    tid_b = body.get("id")
    check("TC-23 再建一个任务绑定同一目标", st == 201 and bool(tid_b), f"HTTP {st} id={tid_b}")

    _, b1 = call("GET", f"/alerts/recent?minutes=60&task_id={tid}", admin)
    _, b2 = call("GET", f"/alerts/recent?minutes=60&task_id={tid_b}", admin)
    i1, i2 = b1.get("items", []), b2.get("items", [])
    check("TC-23 同一告警同属两个任务（N:N 双向可见）",
          len(i1) >= 1 and len(i2) >= 1 and i1[0]["id"] == i2[0]["id"],
          f"A={len(i1)} B={len(i2)}")
    check("TC-23 归属标签同时含两个任务名",
          set(i2[0].get("task_names", [])) >= {"大促保障", "日常巡检"},
          f"names={i2[0].get('task_names')}")

    # ---- TC-24 未知 integration_id：不得阻塞告警接入（§5.3 事实 3）----
    st, body = call("POST", "/alerts/manual", admin, {
        "component": "legacy-svc", "severity": "P3", "title": "未知来源告警",
        "detail": "integration_id 指向不存在的目标", "integration_id": 999999,
    })
    check("TC-24 未知 integration_id → 不抛异常", st == 200 and bool(body.get("alert_id")),
          f"HTTP {st}")
    _, body = call("GET", "/alerts/recent?minutes=60&task_id=none", admin)
    check("TC-24 未知目标告警归入未归属",
          any(i.get("integration_id") == 999999 for i in body.get("items", [])),
          f"n={len(body.get('items', []))}")

    # ---- TC-20 归档：软删除 + 终态守卫 ----
    st, body = call("DELETE", f"/tasks/{tid}", admin)
    check("TC-20 归档 → soft", st == 200 and body.get("soft") is True
          and body.get("deleted") is False and "审计" in body.get("reason", ""), f"HTTP {st} {body}")

    st, body = call("PUT", f"/tasks/{tid}", admin, {"name": "改名试试"})
    check("TC-20 已归档不可再改 → 409 ALREADY_ARCHIVED",
          st == 409 and code_of(body) == "ALREADY_ARCHIVED", f"HTTP {st} {code_of(body)}")

    st, body = call("POST", f"/tasks/{tid}/targets", admin, {"integration_id": ig1})
    check("TC-20 已归档不可改绑定 → 409 TASK_ARCHIVED",
          st == 409 and code_of(body) == "TASK_ARCHIVED", f"HTTP {st} {code_of(body)}")

    st, body = call("GET", "/tasks", admin)
    check("TC-20 默认列表不含已归档",
          all(i["id"] != tid for i in body.get("items", [])), f"ids={[i['id'] for i in body.get('items', [])]}")
    st, body = call("GET", "/tasks?include_archived=true", admin)
    check("TC-20 include_archived 才显示",
          any(i["id"] == tid for i in body.get("items", [])), f"total={body.get('total')}")

    # ---- TC-21 重复归档幂等 ----
    st1, b1 = call("DELETE", f"/tasks/{tid}", admin)
    st2, b2 = call("DELETE", f"/tasks/{tid}", admin)
    check("TC-21 重复归档幂等", st1 == 200 and st2 == 200 and b1 == b2, f"HTTP {st1}/{st2}")

    # ---- TC-22 权限边界 ----
    st, body = call("GET", "/tasks", eng)
    check("TC-22 engineer 列表 → 200", st == 200, f"HTTP {st}")
    st, body = call("POST", "/tasks", eng, {"name": "越权任务", "target_ids": []})
    check("TC-22 engineer 创建 → 403", st == 403, f"HTTP {st}")
    st, body = call("PUT", "/tasks/task_002", eng, {"name": "hack"})
    check("TC-22 engineer 编辑 → 403", st == 403, f"HTTP {st}")
    st, body = call("DELETE", "/tasks/task_002", eng)
    check("TC-22 engineer 归档 → 403", st == 403, f"HTTP {st}")
    st, body = call("POST", "/tasks/task_002/targets", eng, {"integration_id": ig1})
    check("TC-22 engineer 绑定 → 403", st == 403, f"HTTP {st}")
    st, body = call("GET", "/tasks")
    check("TC-22 无 token → 401", st == 401, f"HTTP {st}")

    # ---- 汇总 ----
    n_pass = sum(1 for _, ok, _ in results if ok)
    n_fail = len(results) - n_pass
    print(f"\n===== {n_pass} passed, {n_fail} failed =====")
    raise SystemExit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
