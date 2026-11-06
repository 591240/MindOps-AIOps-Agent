# 智维 MindOps — 告警降噪与可溯源的诊断辅助平台

面向中小团队私有化环境的 AIOps 系统。只做运维链路的后三段：**降噪 → 根因方向 → 处置与经验沉淀**。
不采集指标——时序数据由 Prometheus / Alertmanager 等上游推给它，本项目负责把推来的告警噪声压下去、把结论变得可追溯。

> 一句话定位：**不是让模型更准，而是让模型知道什么时候不该自动收口。**
> 证据不足时系统主动转人工，而不是给出一个看起来很自信的错误答案。

---

## 技术栈

| 层 | 技术 | 说明 |
| :--- | :--- | :--- |
| 前端 | Vue 3.5 + Vite 7 + Vue Router 5 + Tailwind v4 + reka-ui + ECharts | SPA，端口 `3015` |
| 后端 | FastAPI + Tortoise-ORM 0.21.7 + MySQL 8.0 | 端口 `8010` |
| 编排 | LangGraph `StateGraph` + MySQL 检查点（`langgraph-checkpoint-mysql`） | 规则短路 → Context → Diagnose →（置信度回环重试）→ Remediate → Ticket → Recap；人工闸口 `interrupt()` 暂停，确认后从断点续跑 |
| 检索 | BM25（jieba 内存倒排）+ Milvus 向量，RRF 融合 | 任一路不可用可独立降级，不抛错 |
| 可选 | Redis（去重 / 限流 / 跨副本锁）、Milvus（向量） | 单机可缺省，但**多副本必须配 Redis** |

## 数据链路

```
Alertmanager webhook
  → X-Webhook-Token 校验 + 内网 IP 白名单 + 令牌桶限流（全局 100 QPS / 单 IP 30 QPS）
  → 字段归一化 + 指纹去重（SHA1(source|component|title)，带冷却窗口）
  → 事件聚类 / 重聚类（幂等，带审计表）
  → 分阶段诊断（规则层优先，命中即短路；未命中走 LLM + 置信度回环）
  → SSE 实时推送前端（事件时间线、证据、处置建议）
  → 人工确认 → 病历四段式 → 回灌知识库（content_hash 幂等，入库失败事务补偿）
```

## 评测（可复现）

```bash
cd mindops-backend
.venv/Scripts/python.exe scripts/eval_diagnosis.py --all -v --json eval_result.json
```

36 样本（easy / middle / hard 各 12），命中口径 = 结论包含期望关键词；"收口后"指只统计 `confidence` 达阈的作答。

| 模式 | 确定命中 | Brier | ECE | AUC（置信度能否区分对错） | 阈值收口后精确率 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `rule`（仅规则层） | 13/36 | 0.026 | 0.008 | 0.978 | `≥0.6` → 13/14 (93%) |
| `single`（LLM 单轮） | 26/36 | 0.251 | 0.254 | 0.815 | `≥0.6` → 14/15 (93%) |
| `loop`（置信度回环） | 23/36 | **0.120** | **0.150** | **0.925** | `≥0.6` → 18/19 (95%)，`≥0.7` → 14/15 (93%) |

**怎么读这张表**（比第一列更重要）：

- 回环把确定命中率从 72.2% **降到** 63.9%，但把置信度对"答对没有"的判别力从 AUC 0.815 **提到** 0.925。它的收益不是"更准"，而是"知道自己什么时候不准"——所以本系统按阈值收口，达不到的显式转人工。
- 三个必须自己先说的局限：① 样本仅 36 条，93% 的置信区间约 ±14 个百分点，只能当趋势；② 这一轮跑在 Milvus 未启用的环境，日志里 `fusion_method` 全是 `BM25_ONLY` / `EMPTY`，**向量检索与知识回灌的贡献没有被计入**；③ `rule` 那行的 Brier 0.026 主要来自它对 22 条直接弃权（`confidence=0`），不能与另两档横向比较。

**2026-10-22 复核记录**（旧报告已存为 `eval_report.2026-09-12.bak.txt`）：

- 评测集已从 36 条扩到 **42 条**（hard 档 12→18），所以新数字与上表**不可横向比较**：本轮 `rule 13/42`、`single 35/42`、`loop 35/42`。
- 回环确实触发（26/42 跑满 `max_retry`），但命中数与 `single` 逐格相同；差异全在校准侧——置信度均值 0.489→0.649、失败信号 31→12、`no_cross_validation` 归零。
- 局限 ② 的向量部分已补上环境：Milvus 改用 Lite（纯英文路径 `E:/mindops-data/milvus_lite.db`，项目路径含中文会让 faiss 静默失败），`scripts/reindex_knowledge.py` 重建 6 个 `is_latest` 切片，检索实测 `fusion_method=RRF / degraded=false`。但**本轮 eval 仍跑在 dense 关闭时**，向量增益未计入。
- 新发现的量具缺陷（必须先修再谈指标）：`data/eval/golden_set.json` 50 条**全部缺 `expected_docs`**，`eval_retrieval.py` 因此退化为"返回结果里有没有 component 对得上的东西"，而查询本身已按 component 过滤 → 只要不全空就必然命中。实测 dense 开与关的 MRR@10 / Hit@5 / Recall@10 **三档完全相同（0.60/0.60/0.60）**，三个指标彼此相等本身就是征兆。当前这套检索指标**不能**用来证明向量路有价值。

## 快速开始

```bash
# 1) 依赖：MySQL 8.0；配置从 .env.example 复制
cd mindops-backend && cp .env.example .env

# 生产/CI 用锁定版本安装（可复现）；本地开发可用 requirements.txt
.venv/Scripts/python.exe -m pip install -r requirements.lock

# 2) 后端（启动时自动建表，无需手工迁移）
.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8010

# 3) 前端
cd ../mindops-project
node node_modules/vite/bin/vite.js --port 3015 --strictPort
```

测试：`cd mindops-backend && pytest -q`（179 用例，需本地 MySQL；每个模块绑定独立测试库）。
Redis 分支用例在无 Redis 时自动 skip（`MINDOPS_REDIS_TEST_URL` 可指定实例），不会假红。
集成冒烟：`scripts/e2e_api_smoke.py`——注意它会产生**真实模型调用**并写入数据，请在一次性数据库上跑（`MINDOPS_MYSQL_DB=...`）。

**账号怎么来**：开着注册时，注册产出的账号一律是 `engineer`；管理员只发给 `MINDOPS_BOOTSTRAP_ADMIN_EMAIL` 指定的那一个邮箱。本项目不预置公共演示口令。

## 生产部署（Docker Compose）

仓库根目录提供单机生产编排：`docker-compose.yml`（MySQL + Redis + 后端 + Nginx 前端，只有前端暴露端口）、前端 `Dockerfile` / `nginx.conf`、后端 `.env.production.example`。完整步骤——密钥生成、首个管理员、HTTPS、备份恢复、升级与排障——见 [`DEPLOY.md`](DEPLOY.md)。

```bash
cp mindops-backend/.env.production.example mindops-backend/.env.production
# 填写域名、密钥、SMTP 后：
docker compose --env-file mindops-backend/.env.production up -d --build
```

## 生产环境必填项

`MINDOPS_DEBUG=false` 时启动会做安全校验，任一项不合格直接拒绝启动（不静默带病上线）：

| 变量 | 要求 |
| :--- | :--- |
| `MINDOPS_WEBHOOK_TOKEN` | 必填。Alertmanager 侧 `webhook_configs[].http_config.authorization.credentials` 填同值 |
| `MINDOPS_JWT_SECRET` | 必填强随机 |
| `MINDOPS_BOOTSTRAP_ADMIN_EMAIL` | 开注册时必填，否则系统内不会有任何管理员 |
| `MINDOPS_EMAIL_PROVIDER=smtp` + 4 项凭据 | 开注册/邮箱验证码登录时必填（`mock` 在生产会被启动校验拦住） |
| `MINDOPS_CORS_ORIGINS` | 同源反代部署可留空；跨域部署填真实域名，禁止 `*`。生产启动校验会拒绝任何 `localhost` / `127.0.0.1` 来源 |
| `MINDOPS_METRICS_TOKEN` | 可选。设置后 `/metrics` 要求请求头 `X-Metrics-Token`；留空时生产只允许私网/回环来源访问 |
| `MINDOPS_REDIS_URL` | **多副本必填**：限流 / 去重 / SSE 广播跨副本都依赖它；但事件锁与诊断并发信号量仍是进程内对象，见「已知边界」 |
| `config/webhook_whitelist.yaml` | `trusted_proxies` 只登记真正那台反代的地址；填整段私网等于允许客户端伪造 `X-Forwarded-For` 绕过白名单 |

前端生产构建需要 `VITE_API_BASE_URL=/api/v1` 并由网关反代到后端（默认值是 `http://localhost:8010/api/v1`，仅适合本地开发）。SSE 连接改为先用 `/auth/stream-ticket` 换 300 秒短时票据再连流，长效 access_token 不再出现在 URL 里；仓库自带的 `nginx.conf` 访问日志也只记录 `$uri`，不落 query。

## 已知边界（不是没做完，是明确的取舍）

- **没有指标数据面**：诊断上下文来自告警文本 + 知识库，系统取不到时序指标。需要 CPU / 磁盘 / 延迟曲线时应配合 Netdata 或 Prometheus，本项目当前把它们当上游而非数据源。
- **向量层可选**：Milvus 缺失时检索降级为 BM25 并在日志与响应的 `degraded` 字段里如实标注，不假装是全量融合。
- **多副本需 Redis，但诊断侧仍是单副本语义**：限流桶、去重锁、SSE 广播在配了
  `MINDOPS_REDIS_URL` 后可跨副本（SSE 走 Pub/Sub 转发）；但**事件锁**与**诊断并发
  信号量**仍是进程内对象，多 worker 会对同一事件重复起诊断。因此 `--workers 1` 是
  硬约束，横向扩容前必须先把这两处改成 Redis 实现。
- **验证码通道**：`email_provider=mock` 会把验证码经 `debug_code` 回传，只允许在 debug 档使用，生产启动校验会拦。

## 目录

```
mindops-backend/   FastAPI 后端（app/ 按功能分模块，tests/ 179 用例，scripts/ 评测与冒烟）
mindops-project/   Vue 3 前端（src/api 对应后端路由，src/components 业务页，src/router 路由与守卫）
```
