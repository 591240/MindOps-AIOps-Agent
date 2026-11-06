# MindOps 生产部署指南（Docker Compose 单机）

> 适用版本：2026-10（Vue 3 前端 + FastAPI 后端）。
> 目标环境：单台 2C4G 以上 Linux 云服务器，Docker 24+ / Docker Compose v2。
> 架构：Nginx（静态前端 + `/api` 反代）→ FastAPI（**单 worker**）→ MySQL 8 + Redis 7 + Milvus Lite（容器内文件）。

---

## 1. 部署产物

| 文件 | 作用 |
| --- | --- |
| `docker-compose.yml` | 生产编排：mysql / redis / backend / frontend |
| `mindops-backend/Dockerfile` | 后端镜像（非 root，单 worker） |
| `mindops-backend/.env.production.example` | 生产配置模板（复制为 `.env.production`） |
| `mindops-project/Dockerfile` | 前端多阶段构建（Node 构建 → Nginx 托管） |
| `mindops-project/nginx.conf` | SPA 回退 + API 反代 + SSE 长连接 + 日志脱敏 |
| `mindops-backend/config/webhook_whitelist.yaml` | webhook IP 白名单与可信反代地址 |

不要使用 `docker-compose.dev.yml`：它面向本机开发，`DEBUG=true` 且会注入固定口令的演示管理员。

---

## 2. 启动前准备

```bash
cd /path/to/智维
cp mindops-backend/.env.production.example mindops-backend/.env.production
```

编辑 `mindops-backend/.env.production`，至少填写：

```bash
# 数据库
MINDOPS_MYSQL_PASSWORD=<强密码>
MINDOPS_MYSQL_ROOT_PASSWORD=<另一个强密码>

# 三个密钥（分别生成）
python -c "import secrets;print(secrets.token_urlsafe(48))"          # JWT
python -c "import secrets;print(secrets.token_urlsafe(32))"          # Webhook
python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"  # Vault

# 邮件（QQ 邮箱填授权码，不是登录密码）
MINDOPS_EMAIL_SMTP_USER=you@qq.com
MINDOPS_EMAIL_SMTP_PASSWORD=<16 位授权码>

# 公网域名；同源反代部署可留空
MINDOPS_CORS_ORIGINS=https://your-domain.example.com
```

关键约束：

- `MINDOPS_DEBUG=false`。
- `MINDOPS_REGISTER_ENABLED=false`（公网默认关闭自助注册）。
- **不要设置** `MINDOPS_SEED_DEMO_USERS`。
- `MINDOPS_CORS_ORIGINS` 不能再保留 `localhost`：生产启动校验会直接拒绝启动。
- `MINDOPS_METRICS_TOKEN` 仅在需要从公网抓 Prometheus 时设置；未设置时只允许回环/私网访问 `/metrics`。
- 国内服务器建议把 `MINDOPS_PIP_INDEX_URL` 改为 `https://pypi.tuna.tsinghua.edu.cn/simple`，否则后端镜像构建可能在 pypi.org 长时间卡住。

`.env.production` 含密钥，**绝不要提交到 Git**。

---

## 3. 启动

```bash
docker compose --env-file mindops-backend/.env.production up -d --build
docker compose --env-file mindops-backend/.env.production ps
```

后续命令建议加别名：

```bash
alias dc='docker compose --env-file mindops-backend/.env.production'
```

验证：

```bash
curl -s http://127.0.0.1/health          # 经 Nginx → 后端
curl -s http://127.0.0.1/api/v1/health   # 不存在，404 正常
docker compose logs -f backend
```

首次启动会自动建库表（`init_db()`），不需要手工执行迁移命令。

---

## 4. 创建第一个管理员

生产不播种演示账号，注册默认关闭。首次部署按下面步骤建管理员：

1. 临时把 `.env.production` 改为：
   ```bash
   MINDOPS_REGISTER_ENABLED=true
   MINDOPS_BOOTSTRAP_ADMIN_EMAIL=you@qq.com
   ```
2. 重启后端：`dc up -d backend`
3. 打开站点注册页，用该邮箱完成注册 —— 只有这个邮箱会拿到 `admin`，其他注册一律 `engineer`。
4. 立刻改回：
   ```bash
   MINDOPS_REGISTER_ENABLED=false
   MINDOPS_BOOTSTRAP_ADMIN_EMAIL=
   ```
5. 重启后端：`dc up -d backend`

之后如需新增账号，由管理员在系统设置中创建或临时开启注册。

---

## 5. 配置真实模型（否则演示是假的）

新库的 LLM provider 是占位记录（`api_key_enc=NULL`），不配置时：

- 诊断走 stub 返回固定文本；
- 知识库 embedding 用 sha256 派生伪向量，检索退化为 BM25。

上线前用管理员登录「系统设置 → 模型」：

1. 为 `chat`、`reasoning`、`embedding` 三个角色各配置一个模型并点「测试」。
2. embedding 模型维度必须是 **1024**（与 Milvus collection 一致）。
3. 若之前上传过知识，配置真实 embedding 后需要重新入库，否则新旧向量语义不一致。

---

## 6. HTTPS

推荐二选一：

- **前置一层 TLS 网关**（云负载均衡 / Caddy / 宿主机 Nginx），把 443 转发到本 compose 的 80 端口；
- **容器内终止 TLS**：把证书挂载进 `frontend` 容器，在 `mindops-project/nginx.conf` 增加 `listen 443 ssl` 的 server 块，并在 compose 中映射 `443:443`。

无论哪种方式，**不要**把后端 8010、MySQL 3306、Redis 6379 暴露到公网。

如果 80 端口前面还有一层云负载均衡/宿主 Nginx，把它也加进 `config/webhook_whitelist.yaml` 的 `trusted_proxies`；否则后端看到的来源会退化成那一层的地址，登录限流、验证码 4 条/分钟等按 IP 的配额会被所有用户共用。

---

## 7. 备份与恢复

```bash
# MySQL 逻辑备份
dc exec -T mysql sh -c 'exec mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" \
  --single-transaction --routines mindops' > mindops-$(date +%F).sql

# Milvus Lite 数据卷
docker run --rm -v mindops_milvus_data:/data -v "$PWD":/backup alpine \
  tar czf /backup/milvus-$(date +%F).tgz -C /data .
```

恢复：先备份现状，再把 SQL 导入 MySQL、把 Milvus 卷解压回数据卷，最后重启 backend。

建议把备份文件放到对象存储或另一台机器，不要只留在同一块盘上。

---

## 8. 升级

```bash
# 拉取新代码后
dc up -d --build
```

- 后端启动自动执行幂等建表/迁移；
- 前端版本号变化后需要重新构建镜像；
- 升级前先备份 MySQL 与 Milvus 卷。

---

## 9. 日志与监控

```bash
dc logs -f --tail=200 backend
dc logs -f --tail=200 frontend
```

- `/health`：DB 可达性探针，容器健康检查使用。
- `/metrics`：Prometheus 指标。未设置 `MINDOPS_METRICS_TOKEN` 时仅回环/私网可访问；公网抓取请设置 token 并带 `X-Metrics-Token` 头。它**不在** Nginx 的 `/api` 反代范围内：宿主机 Prometheus 抓取时，给 backend 增加 `127.0.0.1:8010:8010` 端口映射，或在前置网关单独反代 `/metrics` 并带上 token。
- Nginx 访问日志只记录路径、不记录 query（SSE 短票不会落盘）；后端镜像已用 `--no-access-log` 关闭 uvicorn 的 URL 访问日志，业务事件日志不受影响。

---

## 10. 与旧版指南的差异（重要）

旧文档中的以下内容已失效，照做会直接失败或引入并发 bug：

| 旧文档 | 现状 |
| --- | --- |
| React 19 | 前端是 Vue 3 + Vite 7 |
| `gunicorn + uvicorn 4 workers` | 不存在 gunicorn；必须 `--workers 1`（事件锁、诊断信号量、限流/去重为进程内对象） |
| `uv` / `pyproject.toml` | 使用 `requirements.txt`，Docker 构建 |
| `python -m app.db migrate` | 不存在；启动时自动建表/迁移 |
| `ensure_collection()` | 不存在；知识入库时自动创建 collection |
| `cleanup_expired_codes()` | 实际是 `clear_expired()`，已由后端定时任务每 6 小时清理 |
| Nginx 只设 `X-Real-IP` | 必须同时设 `X-Forwarded-For`，并把反代地址登记到 `trusted_proxies` |

---

## 11. 故障排查

| 现象 | 排查 |
| --- | --- |
| backend 启动即退出 | `docker compose logs backend` 看配置校验；生产会拒绝弱 DB 密码、空 JWT/Webhook、mock 邮件、localhost CORS 等 |
| 页面打开但接口 502 | backend 是否 healthy；`dc ps`；Nginx 反代地址是否为 `backend:8010` |
| 实时流不推送 | Nginx 是否关闭 `proxy_buffering`；浏览器开发者工具看 `/api/v1/stream/alerts` 是否 401（短票过期会重连换票） |
| 登录报跨域错误 | `MINDOPS_CORS_ORIGINS` 必须是实际访问来源；同源反代可留空 |
| 注册 403 | `MINDOPS_REGISTER_ENABLED=false`，按第 4 节临时开启或由管理员建号 |
| 验证码收不到 | SMTP 授权码/端口 465；服务器出网是否放行；`dc logs backend` 看 `auth.email_send_failed` |
| 知识上传 500 | Milvus 路径必须纯 ASCII；模型 embedding 配置是否正确 |
| 429 频繁 | 验证码按 IP 4 条/分钟；登录按 IP 30 次/5 分钟；检查反代 XFF 是否被正确解析 |

---

## 12. 上线安全检查清单

- [ ] `MINDOPS_DEBUG=false`
- [ ] 三个密钥均为新生成，且与开发环境不同
- [ ] `MINDOPS_REGISTER_ENABLED=false`，`MINDOPS_SEED_DEMO_USERS` 未设置
- [ ] `MINDOPS_CORS_ORIGINS` 不含 localhost
- [ ] `MINDOPS_WEBHOOK_TOKEN` 已设置
- [ ] `trusted_proxies` 只登记 Nginx 固定地址（`172.28.0.10/32`）
- [ ] 后端/数据库/Redis 端口未映射到公网
- [ ] HTTPS 已启用，证书有效期已纳入监控
- [ ] MySQL 与 Milvus 已配置定期备份
- [ ] 已配置真实 LLM/embedding 并完成一次端到端诊断
- [ ] 已用普通账号验证无法访问管理端点（`/metrics`、`/settings`）
