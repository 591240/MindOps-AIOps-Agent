"""F-01 冒烟测试脚本（对应文档 §9 自测矩阵）。"""
import httpx
import json

BASE = "http://127.0.0.1:8010/api/v1"
PASS = 0
FAIL = 0


def _webhook_token() -> str:
    """webhook 共享令牌：优先环境变量，其次读同目录 .env。

    /alerts/webhook 现在强制校验 X-Webhook-Token，冒烟脚本必须带上同一个值。
    """
    import os
    import pathlib

    v = os.environ.get("MINDOPS_WEBHOOK_TOKEN", "").strip()
    if v:
        return v
    env_p = pathlib.Path(__file__).resolve().parents[1] / ".env"
    if env_p.exists():
        for ln in env_p.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if ln.startswith("MINDOPS_WEBHOOK_TOKEN="):
                return ln.split("=", 1)[1].strip()
    return ""


HOOK_H = {"X-Webhook-Token": _webhook_token()}


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}  {detail}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail}")


c = httpx.Client(base_url=BASE, timeout=10)

print("=" * 60)
print("A. 鉴权")
print("=" * 60)

# A1 登录 admin
r = c.post("/auth/login", json={"username": "sre_admin", "password": "Admin@123"})
check("admin 登录 200", r.status_code == 200, f"HTTP={r.status_code}")
admin_token = r.json().get("access_token", "") if r.status_code == 200 else ""
check("admin 返回 token", bool(admin_token))
check("admin role=admin", r.status_code == 200 and r.json().get("user", {}).get("role") == "admin", r.text[:80] if r.status_code != 200 else "")

# A2 登录 engineer
r = c.post("/auth/login", json={"username": "sre001", "password": "P@ssw0rd!"})
check("engineer 登录 200", r.status_code == 200, f"HTTP={r.status_code}")
eng_token = r.json().get("access_token", "") if r.status_code == 200 else ""

# A3 错误密码
r = c.post("/auth/login", json={"username": "sre_admin", "password": "wrong"})
check("错误密码 401", r.status_code == 401, f"HTTP={r.status_code} code={r.json().get('detail',{}).get('code')}")

admin_h = {"Authorization": f"Bearer {admin_token}"}
eng_h = {"Authorization": f"Bearer {eng_token}"}

print()
print("=" * 60)
print("B. 主动上报 POST /alerts")
print("=" * 60)

# B1 正常上报
payload = {"component": "payment-gateway", "severity": "critical", "title": "支付网关 P99 延迟飙升", "detail": "P99 从 200ms 升至 3s"}
r = c.post("/alerts", json=payload, headers=admin_h)
check("上报 200", r.status_code == 200, f"HTTP={r.status_code}")
body = r.json() if r.status_code == 200 else {}
check("accepted=1", body.get("accepted") == 1, str(body))
check("deduped=False", body.get("deduped") is False)
first_alert_id = body.get("alert_ids", [None])[0]
check("返回 alert_id", first_alert_id is not None)

# B2 重复上报（应去重）
r2 = c.post("/alerts", json=payload, headers=admin_h)
b2 = r2.json()
check("重复上报 deduped=True", b2.get("deduped") is True, str(b2))
check("重复上报 accepted=0", b2.get("accepted") == 0, str(b2))

# B3 title 为空（应 400）
r = c.post("/alerts", json={"component": "x", "severity": "critical", "title": ""}, headers=admin_h)
check("空 title 400", r.status_code == 400, f"HTTP={r.status_code}")

# B4 无 token（应 401）
r = c.post("/alerts", json=payload)
check("无 token 401", r.status_code == 401, f"HTTP={r.status_code}")

print()
print("=" * 60)
print("C. 手动注入 POST /alerts/manual")
print("=" * 60)

# C1 admin 手动注入
r = c.post("/alerts/manual", json={"component": "mysql-primary", "severity": "warning", "title": "MySQL 主库磁盘 85%"}, headers=admin_h)
check("admin 手动注入 200", r.status_code == 200, f"HTTP={r.status_code}")
check("返回 pipeline_triggered", r.status_code == 200 and "pipeline_triggered" in r.json(), str(r.json() if r.status_code == 200 else ""))

# C2 engineer 手动注入（应 403，仅 admin）
r = c.post("/alerts/manual", json={"component": "mysql-primary", "severity": "warning", "title": "engineer 越权"}, headers=eng_h)
check("engineer 手动注入 403", r.status_code == 403, f"HTTP={r.status_code}")

print()
print("=" * 60)
print("D. Prometheus webhook POST /alerts/webhook")
print("=" * 60)

webhook_payload = {
    "version": "4",
    "status": "firing",
    "receiver": "webhook",
    "alerts": [
        {
            "status": "firing",
            "labels": {"alertname": "HighCPU", "instance": "10.0.0.5:9100", "severity": "critical"},
            "annotations": {"summary": "CPU 使用率 > 90%", "description": "持续 5 分钟"},
            "startsAt": "2026-09-08T02:00:00Z",
        }
    ],
}
# D0 缺令牌必须 401（webhook 共享密钥鉴权）
r = c.post("/alerts/webhook", json=webhook_payload)
check("webhook 无令牌 401", r.status_code == 401,
      f"HTTP={r.status_code} code={r.json().get('detail', {}).get('code') if r.status_code == 401 else ''}")

r = c.post("/alerts/webhook", json=webhook_payload, headers=HOOK_H)
check("webhook 200（本机白名单 + 令牌）", r.status_code == 200, f"HTTP={r.status_code}")
check("webhook accepted>=1", r.status_code == 200 and r.json().get("accepted", 0) >= 1, r.text[:100] if r.status_code != 200 else "")

# D2 重复 webhook（应去重）
r2 = c.post("/alerts/webhook", json=webhook_payload, headers=HOOK_H)
check("重复 webhook deduped", r2.status_code == 200 and r2.json().get("deduped") is True, r2.text[:100] if r2.status_code != 200 else "")

# D3 非法 JSON（应 400）
r = c.post("/alerts/webhook", content=b"not json",
           headers={**HOOK_H, "Content-Type": "application/json"})
check("非法 JSON 400", r.status_code == 400, f"HTTP={r.status_code}")

print()
print("=" * 60)
print("E. 最近告警 GET /alerts/recent")
print("=" * 60)

r = c.get("/alerts/recent", headers=eng_h)
check("recent 200", r.status_code == 200, f"HTTP={r.status_code}")
check("recent 有数据", r.status_code == 200 and r.json().get("total", 0) >= 1, str(r.json() if r.status_code == 200 else ""))
check("recent 字段完整", r.status_code == 200 and "items" in r.json() and "total" in r.json())

# E2 无 token
r = c.get("/alerts/recent")
check("recent 无 token 401", r.status_code == 401, f"HTTP={r.status_code}")

# E3 severity 过滤（入库后已归一化为 P0/P1/P2/P3 枚举）
r = c.get("/alerts/recent", params={"severity": "P0"}, headers=eng_h)
check("severity=P0 过滤", r.status_code == 200 and all(i["severity"] == "P0" for i in r.json().get("items", [])), f"total={r.json().get('total')}")

print()
print("=" * 60)
print("F. 健康检查 / 限流探针")
print("=" * 60)

r = httpx.get("http://127.0.0.1:8010/health")
check("health 200", r.status_code == 200 and r.json().get("status") == "ok")

# 快速连发 40 次触发单 IP 限流（阈值 30 QPS）
limited = False
for _ in range(40):
    r = c.post("/alerts/webhook", json=webhook_payload, headers=HOOK_H)
    if r.status_code == 429:
        limited = True
        break
check("单 IP 限流 429 触发", limited, "连发 40 次命中 429")

print()
print("=" * 60)
print(f"结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 60)
c.close()
