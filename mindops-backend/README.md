# MindOps 后端

FastAPI + Tortoise-ORM 0.21.7 + MySQL 8。总览、数据链路、评测结果与生产配置表见仓库根目录 [`README.md`](../README.md)。

## 运行

```bash
pip install -r requirements.txt          # Python 3.13
cp .env.example .env                      # 按需改值；.env 不入库
python -m uvicorn app.main:app --host 127.0.0.1 --port 8010
```

- 首次启动自动建表（`app/db.py` 幂等 DDL + 增量 ALTER），不需要手工迁移。
- 健康检查 `GET /health`；指标 `GET /metrics`（Prometheus 文本）。生产环境默认只允许私网/回环来源；配置 `MINDOPS_METRICS_TOKEN` 后改为校验请求头 `X-Metrics-Token`，不匹配统一返回 404。

## 测试

```bash
pytest -q            # 179 用例；需要本地 MySQL，每个模块绑定各自的测试库
```

`tests/smoke_f01.py` 与 `scripts/e2e_*.py` 是打到**运行中服务**的冒烟脚本，会写入数据并触发真实模型调用；请在一次性数据库上跑（`MINDOPS_MYSQL_DB=...`），不要对着演示库执行。

## 鉴权与凭据边界

- 账号来源：开启注册时，注册产出 `engineer`；管理员只发给 `MINDOPS_BOOTSTRAP_ADMIN_EMAIL` 指定的邮箱。仓库不对外提供演示口令。
- 演示种子账号只在显式设置 `MINDOPS_SEED_DEMO_USERS=1` 时写入（`DEBUG=true` 也不会自动播种）；公网部署**不要**打开。
- `/alerts/webhook` 除内网 IP 白名单外，还需请求头 `X-Webhook-Token`（`MINDOPS_WEBHOOK_TOKEN`）。生产环境该值留空会直接拒绝启动。
- `config/webhook_whitelist.yaml` 的 `trusted_proxies` 只登记真正那台反向代理的地址；写成整段私网会让客户端自填 `X-Forwarded-For` 就能同时过白名单和换取限流分桶。

## 配置要点（`MINDOPS_` 前缀，完整清单见 `.env.example`）

| 变量 | 说明 |
| :--- | :--- |
| `MINDOPS_DEBUG` | `false` 时启动做安全校验，任一项不合格拒绝启动 |
| `MINDOPS_JWT_SECRET` | 生产必填；debug 下留空会生成临时随机密钥（重启即失效） |
| `MINDOPS_WEBHOOK_TOKEN` | 生产必填（webhook 共享令牌） |
| `MINDOPS_BOOTSTRAP_ADMIN_EMAIL` | 开注册时必填，否则系统内没有管理员 |
| `MINDOPS_REGISTER_ENABLED` / `MINDOPS_EMAIL_PROVIDER` | 自助注册开关与邮件通道（注册验证码 + 邮箱验证码登录共用；`mock` 仅 debug 可用） |
| `MINDOPS_REDIS_URL` | 留空走进程内等价实现；**多副本必须配**（限流 / 去重 / SSE 广播）；事件锁与诊断并发信号量仍是进程内对象，`--workers 1` 是硬约束 |
| `MINDOPS_METRICS_TOKEN` | `/metrics` 的 `X-Metrics-Token`；留空时生产只允许私网/回环访问 |
| `MINDOPS_STREAM_TICKET_TTL_SECONDS` | SSE 短时票据有效期，默认 300 秒 |
| `MINDOPS_MILVUS_URI` | 留空则向量检索降级为 BM25；本地路径不能含非 ASCII 字符（faiss 限制） |
| `MINDOPS_LLM_VAULT_KEY` | Fernet 主密钥，模型 API Key 落库加密用；更换需重新加密存量行 |
| `MINDOPS_RATE_GLOBAL_QPS` / `MINDOPS_RATE_IP_QPS` | 100 / 30 |
| `MINDOPS_DIAGNOSIS_MAX_CONCURRENT_RUNS` 等 | 诊断并发数、排队超时、心跳中断判定 |

## 模块地图

| 目录 | 职责 |
| :--- | :--- |
| `app/alerts/` | 接入、归一化、指纹去重、限流、IP 白名单与令牌校验、SSE 广播 |
| `app/incidents/` | 聚类 / 重聚类（幂等 + 审计）与事件服务 |
| `app/agents/` | 分阶段编排：规则短路、Context、Diagnose、置信度回环、Remediate、Ticket、Recap |
| `app/rag/` | BM25（jieba 内存倒排）、embedding、Milvus 客户端、RRF 融合与降级链 |
| `app/knowledge/` | 文档切片入库、幂等与失败补偿 |
| `app/auth/` | JWT、口令哈希、邮箱验证码（注册 + 登录）、黑名单 |
| `app/llm/` | 模型注册表、网关调用与降级链、Fernet 保险库 |
| `app/integrations/` | 上游集成与 SSRF 校验链 |
| `scripts/eval_diagnosis.py` | 评测集跑分与置信度校准统计（详见根 README 的评测章节） |

各功能的专项设计文档未随本仓库发布。
