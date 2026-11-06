"""Tortoise-ORM 初始化 + 建表。

设计说明：
  文档 §4.2 / §4.4 的 DDL 含 CHECK 约束、MEDIUMTEXT、CHAR(40) 与复合索引，
  Tortoise 的 generate_schemas 无法精确表达，因此这里用**原生 SQL 建表**，
  保证与文档 DDL 一字不差；Tortoise 仅负责运行时的 ORM 读写。
"""

from __future__ import annotations

import logging

from tortoise import Tortoise

from app.config import settings

logger = logging.getLogger("mindops.db")

# ---------------------------------------------------------------------------
# 幂等 DDL 执行
# ---------------------------------------------------------------------------
# 迁移语句靠"重复执行报错即跳过"来保证幂等。原实现是裸的
# `except Exception: pass`，这会把**真正的失败**（权限不足、SQL 写错、
# 磁盘满、连接中断）一并吞掉，让库停留在半迁移状态且毫无痕迹。
#
# 这里改为只吞"已经存在/不存在"这类预期错误，其余一律记日志后抛出。
_IDEMPOTENT_ERRNOS = {
    1050,  # ER_TABLE_EXISTS_ERROR     表已存在
    1060,  # ER_DUP_FIELDNAME          列已存在
    1061,  # ER_DUP_KEYNAME            索引名已存在
    1091,  # ER_CANT_DROP_FIELD_OR_KEY 待删列/键不存在
    1826,  # ER_FK_DUP_NAME            外键重名
}
_IDEMPOTENT_MARKERS = (
    "Duplicate column name",
    "Duplicate key name",
    "already exists",
    "check that column/key exists",
    "Can't DROP",
    # 注意：这里**不能**把 "Unknown column" / "Unknown table" 当幂等错误。
    # 它们同样会由"列名拼错、表名写错、迁移语句本身有 bug"触发，一旦吞掉，
    # 库会静默停在半迁移状态且日志里毫无痕迹。需要"列可能不存在"的场景，
    # 一律先用 _column_exists() 显式判断，不要靠吞错。
)


def _is_idempotent_error(exc: BaseException) -> bool:
    """判断异常是否属于"重复执行导致的预期错误"。"""
    code = exc.args[0] if getattr(exc, "args", None) else None
    if isinstance(code, int) and code in _IDEMPOTENT_ERRNOS:
        return True
    text = str(exc)
    return any(m.lower() in text.lower() for m in _IDEMPOTENT_MARKERS)


async def _column_exists(conn, table: str, column: str) -> bool:
    """判断列是否已存在（用于替代"靠吞 Unknown column 报错"的旧做法）。

    只查当前库：table_schema = DATABASE()，避免同名表在其它库里造成误判。
    """
    rows = await conn.execute_query_dict(
        "SELECT COUNT(*) AS n FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = %s AND column_name = %s",
        [table, column],
    )
    return bool(rows and int(rows[0]["n"]) > 0)


async def _exec_idempotent(conn, sql: str) -> bool:
    """执行幂等 DDL/DML。

    返回 True 表示语句实际生效，False 表示因"已存在/不存在"而跳过。
    非预期错误会记 error 日志并继续抛出 —— 迁移失败必须可见。
    """
    try:
        await conn.execute_query(sql)
        return True
    except Exception as exc:
        if _is_idempotent_error(exc):
            logger.debug("DDL 跳过（已存在或无需变更）：%s", sql[:80])
            return False
        logger.error("DDL 执行失败：%s | 错误：%s", sql[:200], exc)
        raise

# ---- 文档 §4.2 alerts 表（V1.1 修订版）----
SQL_ALERTS = """
CREATE TABLE IF NOT EXISTS alerts (
  id            BIGINT AUTO_INCREMENT PRIMARY KEY,
  source        VARCHAR(32)  NOT NULL,
  component     VARCHAR(64)  NOT NULL,
  severity      VARCHAR(8)   NOT NULL,
  title         VARCHAR(255) NOT NULL,
  detail        TEXT         NOT NULL,
  fingerprint   CHAR(40)     NOT NULL,
  incident_id   INT          NULL,
  raw_payload   MEDIUMTEXT   NULL,
  labels_json   JSON         NULL,
  dedup_count   INT          NOT NULL DEFAULT 1,
  last_seen_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  integration_id INT         NULL,

  CONSTRAINT chk_severity CHECK (severity IN ('P0','P1','P2','P3')),
  CONSTRAINT chk_source   CHECK (source   IN ('prometheus','app','demo')),
  CONSTRAINT chk_comp     CHECK (component REGEXP '^[a-z0-9_-]{1,64}$'),
  CONSTRAINT chk_fpr      CHECK (CHAR_LENGTH(fingerprint) = 40),

  INDEX idx_fingerprint (fingerprint),
  INDEX idx_incident  (incident_id),
  INDEX idx_created   (created_at),
  INDEX idx_sev_comp  (severity, component)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='原始告警接入表（F-01）';
"""

# ---- 文档 §4.4 死信队列表（V1.1 新增）----
SQL_DLQ = """
CREATE TABLE IF NOT EXISTS alerts_failed_queue (
  id            BIGINT AUTO_INCREMENT PRIMARY KEY,
  source        VARCHAR(32)  NOT NULL,
  payload       MEDIUMTEXT   NOT NULL,
  error         TEXT         NOT NULL,
  retry_count   INT          NOT NULL DEFAULT 0,
  next_retry_at DATETIME     NOT NULL,
  resolved      TINYINT(1)   NOT NULL DEFAULT 0,
  created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  resolved_at   DATETIME     NULL,
  INDEX idx_retry (resolved, next_retry_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='告警接入死信队列（F-01 DLQ）';
"""

# ---- incidents：F-02 事件聚合表（§4.1） ----
SQL_INCIDENTS = """
CREATE TABLE IF NOT EXISTS incidents (
  id INT AUTO_INCREMENT PRIMARY KEY,
  fingerprint CHAR(40) NULL,
  title VARCHAR(255) NOT NULL,
  component VARCHAR(64) NOT NULL DEFAULT '',
  severity VARCHAR(8) NOT NULL DEFAULT 'P3',
  status VARCHAR(16) NOT NULL DEFAULT 'new',
  last_seen_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  root_cause VARCHAR(255) NULL,
  root_cause_confidence FLOAT NULL,
  root_cause_source VARCHAR(16) NULL,
  actual_cause VARCHAR(255) NULL,
  resolved_at DATETIME NULL,
  confirmed_by INT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_comp_status_win (component, status, last_seen_at),
  INDEX idx_created (created_at),
  INDEX idx_fingerprint (fingerprint)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='事件表（M1 桩最小结构，F-02 扩展）';
"""

SQL_INCIDENT_RECLUSTER_AUDITS = """
CREATE TABLE IF NOT EXISTS incident_recluster_audits (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  actor VARCHAR(64) NOT NULL,
  phase VARCHAR(16) NOT NULL,
  old_incident_id INT NOT NULL,
  new_incident_id INT NOT NULL,
  details JSON NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_old_incident (old_incident_id),
  INDEX idx_new_incident (new_incident_id),
  INDEX idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='F-02 手动重聚类审计记录';
"""

SQL_INCIDENT_STEPS = """
CREATE TABLE IF NOT EXISTS incident_steps (
  id INT AUTO_INCREMENT PRIMARY KEY,
  run_id VARCHAR(32) NOT NULL DEFAULT '',
  incident_id INT NOT NULL,
  step VARCHAR(16) NOT NULL,
  agent VARCHAR(32) NOT NULL,
  input_summary TEXT NULL,
  output_summary TEXT NULL,
  tool_calls TEXT NULL,
  latency_ms INT NULL,
  confidence FLOAT NULL,
  retry_count INT NOT NULL DEFAULT 0,
  model_id INT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_incident (incident_id),
  INDEX idx_run (run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='F-03 Agent 诊断轨迹';
"""

SQL_DIAGNOSIS_RUNS = """
CREATE TABLE IF NOT EXISTS diagnosis_runs (
  run_id VARCHAR(32) PRIMARY KEY,
  incident_id INT NOT NULL,
  status VARCHAR(16) NOT NULL,
  heartbeat_at DATETIME NULL,
  started_at DATETIME NULL,
  finished_at DATETIME NULL,
  triggered_by INT NULL,
  INDEX idx_incident (incident_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='F-03 诊断运行表';
"""

SQL_EVIDENCE = """
CREATE TABLE IF NOT EXISTS evidence (
  id INT AUTO_INCREMENT PRIMARY KEY,
  incident_id INT NOT NULL,
  step_id INT NULL,
  chunk_id INT NULL,
  quote TEXT NOT NULL,
  source_doc VARCHAR(255) NOT NULL,
  score FLOAT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_incident (incident_id),
  INDEX idx_step (step_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='F-03 诊断证据';
"""

SQL_REMEDIATION_ACTIONS = """
CREATE TABLE IF NOT EXISTS remediation_actions (
  id INT AUTO_INCREMENT PRIMARY KEY,
  incident_id INT NOT NULL,
  command TEXT NOT NULL,
  description VARCHAR(255) NULL,
  risk_level VARCHAR(8) NOT NULL,
  requires_confirm TINYINT(1) NOT NULL DEFAULT 1,
  status VARCHAR(16) NOT NULL DEFAULT 'pending',
  confirmed_by INT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  exec_status VARCHAR(16) NULL,
  exec_exit_code INT NULL,
  exec_output TEXT NULL,
  exec_duration_ms INT NULL,
  executed_at DATETIME NULL,
  INDEX idx_incident (incident_id),
  INDEX idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='F-03 处置建议';
"""

# ---- loop_audits：F-04 置信度回环审计表（§4.3）----
SQL_LOOP_AUDITS = """
CREATE TABLE IF NOT EXISTS loop_audits (
  id              INT AUTO_INCREMENT PRIMARY KEY,
  run_id          VARCHAR(32) NOT NULL,
  incident_id     INT NOT NULL,
  retry_index     INT NOT NULL,
  strategy        VARCHAR(64) NOT NULL,
  query_before    TEXT NULL,
  query_after     TEXT NULL,
  recall_top_k    INT NULL,
  sim_floor       FLOAT NULL,
  component_scope VARCHAR(64) NULL,
  kb_hits_before  INT NULL,
  kb_hits_after   INT NULL,
  model_id        INT NULL,
  role            VARCHAR(16) NOT NULL DEFAULT 'chat',
  conf_before     FLOAT NULL,
  conf_after      FLOAT NULL,
  escalated       TINYINT(1) NOT NULL DEFAULT 0,
  created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_run (run_id),
  INDEX idx_incident (incident_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='置信度回环审计表（F-04）';
"""

# ---- runtime_configs：F-04 运行时参数覆盖表（§4.4）----
SQL_RUNTIME_CONFIGS = """
CREATE TABLE IF NOT EXISTS runtime_configs (
  `key`       VARCHAR(64) PRIMARY KEY,
  `value`     VARCHAR(255) NOT NULL,
  updated_by  VARCHAR(64) NULL,
  updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='运行时参数覆盖表（F-04）';
"""

# ---- llm_providers：F-09 全量模型配置表（PRD §3.11；原 F-04 最小骨架经迁移块升级）----
SQL_LLM_PROVIDERS = """
CREATE TABLE IF NOT EXISTS llm_providers (
  id                  INT AUTO_INCREMENT PRIMARY KEY,
  user_id             INT NULL,
  name                VARCHAR(64) NOT NULL,
  provider_type       VARCHAR(32) NOT NULL DEFAULT 'system',
  base_url            VARCHAR(255) NOT NULL DEFAULT '',
  model_name          VARCHAR(64) NOT NULL DEFAULT '',
  api_key_enc         TEXT NULL,
  api_key_masked      VARCHAR(32) NOT NULL DEFAULT '',
  role                VARCHAR(16) NOT NULL DEFAULT 'chat',
  is_default          TINYINT(1) NOT NULL DEFAULT 0,
  status              VARCHAR(16) NOT NULL DEFAULT 'active',
  temperature         FLOAT NOT NULL DEFAULT 0.2,
  max_tokens          INT NOT NULL DEFAULT 4096,
  timeout_ms          INT NOT NULL DEFAULT 60000,
  last_test_at        DATETIME NULL,
  last_test_latency_ms INT NULL,
  last_test_status    VARCHAR(32) NULL,
  extra_params        TEXT NULL,
  created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_llm_role_default (role, is_default, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='F-09 用户自接模型';
"""

# ---- users：F-01 鉴权依赖所需最小表（完整登录属 F-10）----
SQL_USERS = """
CREATE TABLE IF NOT EXISTS users (
  id                 INT AUTO_INCREMENT PRIMARY KEY,
  username           VARCHAR(64)  NOT NULL UNIQUE,
  hashed_password    VARCHAR(255) NOT NULL,
  role               VARCHAR(16)  NOT NULL DEFAULT 'engineer',
  phone              VARCHAR(20)  NULL     DEFAULT NULL,
  phone_verified     TINYINT(1)   NOT NULL DEFAULT 0,
  status             VARCHAR(16)  NOT NULL DEFAULT 'active',
  last_login         DATETIME     NULL,
  last_login_method  VARCHAR(16)  NULL,
  email              VARCHAR(128) NULL     DEFAULT NULL,
  email_verified     TINYINT(1)   NOT NULL DEFAULT 0,
  created_at         DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_users_phone (phone),
  UNIQUE KEY uk_users_email (email),
  INDEX idx_users_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='F-10 用户表（含邮箱验证码登录字段）';
"""

# ---- 邮箱验证码表（注册 / 登录共用）----
SQL_EMAIL_CODES = """
CREATE TABLE IF NOT EXISTS email_codes (
  id             INT AUTO_INCREMENT PRIMARY KEY,
  email          VARCHAR(128) NOT NULL,
  code_hash      VARCHAR(64)  NOT NULL  COMMENT 'SHA256(code + salt)',
  salt           VARCHAR(32)  NOT NULL,
  attempts       INT          NOT NULL DEFAULT 0,
  max_attempts   INT          NOT NULL DEFAULT 5,
  expires_at     DATETIME     NOT NULL,
  status         VARCHAR(16)  NOT NULL DEFAULT 'pending',
  created_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_email_code_email (email),
  INDEX idx_email_code_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='邮箱注册验证码';
"""

SQL_KNOWLEDGE_DOCS = """
CREATE TABLE IF NOT EXISTS knowledge_docs (
  id INT AUTO_INCREMENT PRIMARY KEY,
  title VARCHAR(255) NOT NULL,
  content MEDIUMTEXT NOT NULL,
  source_type VARCHAR(16) NOT NULL,
  version INT NOT NULL DEFAULT 1,
  chunk_count INT NOT NULL DEFAULT 0,
  is_latest TINYINT(1) NOT NULL DEFAULT 1,
  created_by INT NULL,
  content_hash CHAR(40) NULL,
  component VARCHAR(64) NOT NULL DEFAULT 'common',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uq_knowledge_title_version (title, version),
  INDEX idx_knowledge_latest (is_latest),
  INDEX idx_knowledge_source (source_type),
  INDEX idx_knowledge_doc_component (component)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='F-05 知识库文档';
"""

SQL_KNOWLEDGE_CHUNKS = """
CREATE TABLE IF NOT EXISTS knowledge_chunks (
  id INT AUTO_INCREMENT PRIMARY KEY,
  doc_id INT NOT NULL,
  text TEXT NOT NULL,
  component VARCHAR(64) NOT NULL DEFAULT 'common',
  doc_type VARCHAR(16) NOT NULL,
  chunk_index INT NOT NULL,
  chunk_kind VARCHAR(16) NOT NULL DEFAULT 'text',
  source_version INT NOT NULL DEFAULT 1,
  embedding_id VARCHAR(64) NULL,
  is_latest TINYINT(1) NOT NULL DEFAULT 1,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_knowledge_chunk_doc (doc_id),
  INDEX idx_knowledge_chunk_component (component),
  INDEX idx_knowledge_chunk_latest (is_latest),
  INDEX idx_knowledge_chunk_embedding (embedding_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='F-05 知识库切片';
"""

# ---- F-11 集成管理：被监控目标注册表（§4.1 DDL）----
# ---- F-12 监控任务（30-F-12 §4.1 / §4.2）----
SQL_TASKS = """
CREATE TABLE IF NOT EXISTS tasks (
  id           VARCHAR(32)  NOT NULL PRIMARY KEY,
  name         VARCHAR(64)  NOT NULL,
  description  VARCHAR(255) NOT NULL DEFAULT '',
  status       VARCHAR(16)  NOT NULL DEFAULT 'active',
  target_count INT          NOT NULL DEFAULT 0,
  created_by   VARCHAR(64)  NOT NULL DEFAULT '',
  created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  archived_at  DATETIME     NULL,
  UNIQUE KEY uk_task_name (name),
  INDEX idx_task_status (status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='F-12 监控任务（顶层容器）';
"""

SQL_TASK_TARGETS = """
CREATE TABLE IF NOT EXISTS task_targets (
  id             INT AUTO_INCREMENT PRIMARY KEY,
  task_id        VARCHAR(32) NOT NULL,
  integration_id INT         NOT NULL,
  added_by       VARCHAR(64) NOT NULL DEFAULT '',
  added_at       DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_task_target (task_id, integration_id),
  INDEX idx_tt_integration (integration_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='F-12 任务与被监控目标的绑定关系';
"""

SQL_INTEGRATIONS = """
CREATE TABLE IF NOT EXISTS integrations (
  id              INT AUTO_INCREMENT PRIMARY KEY,
  name            VARCHAR(64)  NOT NULL,
  type            VARCHAR(32)  NOT NULL DEFAULT 'prometheus',
  base_url        VARCHAR(255) NOT NULL,
  scrape_config   VARCHAR(128) NOT NULL DEFAULT '',
  status          VARCHAR(16)  NOT NULL DEFAULT 'pending',
  last_heartbeat  DATETIME     NULL,
  last_error      VARCHAR(255) NULL,
  last_latency_ms INT          NULL,
  targets_total   INT          NULL,
  targets_up      INT          NULL,
  created_by      VARCHAR(64)  NOT NULL DEFAULT '',
  created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  disabled_at     DATETIME     NULL,
  INDEX idx_integration_status (status, type),
  UNIQUE KEY uk_base_scrape (base_url, scrape_config)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='F-11 被监控目标（集成管理）';
"""

# ---- 迁移版本登记表 ----
# 没有它时，init_db() 每次启动都要把几十条 ALTER 重跑一遍：启动慢、多副本
# 同时启动会争 DDL 元数据锁，而且"这条要不要跳过"完全靠吞错来判断。
# 这里用一张极小的登记表做**基线**：同一组迁移只执行一次。
#
# 新增迁移时：把新语句放进新的 `_run_xxx_migrations()`，配一个新的版本号常量，
# 不要往已发布的基线里追加语句——那样老库不会重跑基线，新语句永远不会生效。
SQL_SCHEMA_MIGRATIONS = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version    VARCHAR(64) PRIMARY KEY,
  applied_at DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='已执行的迁移版本登记';
"""

# 当前历史迁移集合的版本号。**只在新增迁移块时改它。**
BASELINE_VERSION = "2026_10_baseline_v1"


async def _migration_applied(conn, version: str) -> bool:
    rows = await conn.execute_query_dict(
        "SELECT 1 AS hit FROM schema_migrations WHERE version = %s", [version]
    )
    return bool(rows)


async def _mark_migration(conn, version: str) -> None:
    await conn.execute_query_dict(
        "INSERT IGNORE INTO schema_migrations (version) VALUES (%s)", [version]
    )


def tortoise_orm() -> dict:
    """构造 Tortoise 配置。

    **必须**在运行时调用（而不是在模块级固化一个常量）：
    `settings.database_url` 依赖环境变量，而环境变量可能在本模块被
    import 之后才确定（典型场景：pytest 按文件名顺序收集，
    `test_f09_api.py` 先于 `test_f11.py` import，二者各自声明了测试库）。
    若在模块级固化，先 import 的模块会把库名"焊死"给整个进程，
    后续模块的 `os.environ` 声明全部失效 —— 表现为测试写到错误的库。
    """
    return {
        "connections": {"default": settings.database_url},
        "apps": {
            "models": {
                "models": ["app.models"],
                "default_connection": "default",
            }
        },
        "use_tz": False,
        "timezone": "Asia/Shanghai",
    }


async def _seed_llm_providers() -> None:
    """F-04 §6.2 依赖：至少注册 chat / reasoning / embedding 三个角色模型占位。

    F-09 V1.0：系统种子不带厂商/密钥（api_key_enc=NULL，调用走桩路径，
    F-09 §5.7 兜底层）；每个 role 唯一且 is_default=True。
    已存在同名条目时跳过，保证幂等。

    默认位只在"该 role 还没有任何默认模型"时才抢：否则每次重启都会和用户
    自己设的默认模型打架（同 role 两个 is_default=True，排序结果不确定）。
    """
    from app.models import LlmProvider

    for name, role in (("general-chat", "chat"), ("deep-reasoning", "reasoning"), ("text-embedding-v3", "embedding")):
        if await LlmProvider.get_or_none(name=name) is not None:
            continue
        has_default = await LlmProvider.filter(role=role, is_default=True).exists()
        await LlmProvider.create(
            name=name, role=role, provider_type="system",
            is_default=not has_default, status="active",
        )


async def _migrate_llm_providers(conn) -> None:
    """F-09 V1.0：把 F-04 最小骨架表（id/name/role/enabled）升级为全量表。

    与 incidents 迁移同策略：逐条 ALTER 失败即跳过（幂等）。步骤：
    ① 补全量列；② enabled 布尔语义搬到 status；③ 种子行补 system/is_default；
    ④ 删除 enabled 列（旧索引 idx_role_enabled 随列自动移除）；⑤ 补新索引。
    """
    columns = {
        "user_id": "INT NULL",
        "provider_type": "VARCHAR(32) NOT NULL DEFAULT 'system'",
        "base_url": "VARCHAR(255) NOT NULL DEFAULT ''",
        "model_name": "VARCHAR(64) NOT NULL DEFAULT ''",
        "api_key_enc": "TEXT NULL",
        "api_key_masked": "VARCHAR(32) NOT NULL DEFAULT ''",
        "is_default": "TINYINT(1) NOT NULL DEFAULT 0",
        "status": "VARCHAR(16) NOT NULL DEFAULT 'active'",
        "temperature": "FLOAT NOT NULL DEFAULT 0.2",
        "max_tokens": "INT NOT NULL DEFAULT 4096",
        "timeout_ms": "INT NOT NULL DEFAULT 60000",
        "last_test_at": "DATETIME NULL",
        "last_test_latency_ms": "INT NULL",
        "last_test_status": "VARCHAR(32) NULL",
        "extra_params": "TEXT NULL",
        "updated_at": "DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP",
    }
    for name, definition in columns.items():
        await _exec_idempotent(conn, f"ALTER TABLE llm_providers ADD COLUMN {name} {definition}")
    # enabled -> status：仅当旧列仍存在时才执行（新装库没有 enabled）。
    # 这里显式查列，而不是让 "Unknown column" 被幂等逻辑吞掉 —— 后者会掩盖
    # 真正的列名错误，让迁移静默失效。
    if await _column_exists(conn, "llm_providers", "enabled"):
        await _exec_idempotent(
            conn,
            "UPDATE llm_providers SET status = CASE WHEN enabled = 1 THEN 'active' ELSE 'disabled' END",
        )
        await _exec_idempotent(conn, "ALTER TABLE llm_providers DROP COLUMN enabled")
    await _exec_idempotent(
        conn,
        "UPDATE llm_providers SET provider_type = 'system', is_default = 1 "
        "WHERE name IN ('general-chat', 'deep-reasoning', 'text-embedding-v3')",
    )
    await _exec_idempotent(
        conn, "CREATE INDEX idx_llm_role_default ON llm_providers (role, is_default, status)"
    )


async def _migrate_users(conn) -> None:
    """F-10 / 邮箱注册：给 users 表补全短信与邮箱字段（幂等 ALTER，已有则跳过）。"""
    columns = {
        "phone": "VARCHAR(20) NULL DEFAULT NULL",
        "phone_verified": "TINYINT(1) NOT NULL DEFAULT 0",
        "status": "VARCHAR(16) NOT NULL DEFAULT 'active'",
        "last_login": "DATETIME NULL",
        "last_login_method": "VARCHAR(16) NULL",
        # 唯一索引不在这里加：历史行可能有重复邮箱，ALTER 会直接失败。
        # 由 _ensure_unique_user_email() 在启动时先查重、再加唯一索引。
        "email": "VARCHAR(128) NULL DEFAULT NULL",
        "email_verified": "TINYINT(1) NOT NULL DEFAULT 0",
    }
    for name, definition in columns.items():
        await _exec_idempotent(conn, f"ALTER TABLE users ADD COLUMN {name} {definition}")
    # 补索引
    await _exec_idempotent(conn, "CREATE INDEX idx_users_phone ON users (phone)")
    await _exec_idempotent(conn, "CREATE INDEX idx_users_email ON users (email)")


async def _ensure_unique_user_email(conn) -> None:
    """给 users.email 补唯一索引（每次启动幂等执行）。

    并发注册同一邮箱时，注册接口的 exists() 查重存在竞态窗口，唯一索引是
    最终兜底。历史库若有重复邮箱则跳过并留痕，避免启动直接失败；
    清理重复数据后重启即可补上。
    """
    rows = await conn.execute_query_dict(
        "SELECT email FROM users WHERE email IS NOT NULL "
        "GROUP BY email HAVING COUNT(*) > 1 LIMIT 1"
    )
    if rows:
        logger.warning(
            "users.email 存在重复值，跳过唯一索引 uk_users_email；"
            "清理重复邮箱后重启服务即可补上。"
        )
        return
    await _exec_idempotent(
        conn, "CREATE UNIQUE INDEX uk_users_email ON users (email)"
    )


async def init_db() -> None:
    """初始化连接并按文档 DDL 建表（幂等）。

    容错说明：`Tortoise.init()` 内部会先 `connections.close_all(discard=True)`
    去清理上一次的连接。如果上一个连接挂在**已经被关闭的事件循环**上
    （典型场景：测试里连续多个 `TestClient` / 模块级 loop，前一个循环已销毁），
    这个"清理"动作本身就会抛 `RuntimeError: Event loop is closed`，
    导致整个模块的用例在 setup 阶段全部 ERROR。

    此时旧连接已随循环一起释放，没有资源需要回收，
    因此这里在调用前先丢弃 Tortoise 的连接注册表，
    并在 `Tortoise.init` 仍失败时降级重试一次。
    """
    import contextlib

    await _reset_connections()
    try:
        await Tortoise.init(config=tortoise_orm())
    except RuntimeError as exc:
        if "Event loop is closed" not in str(exc):
            raise
        # 兜底：旧循环相关状态仍在，丢掉注册表后再试一次
        with contextlib.suppress(Exception):
            await Tortoise._reset_apps()  # type: ignore[attr-defined]
        await _reset_connections()
        await Tortoise.init(config=tortoise_orm())

    conn = Tortoise.get_connection("default")
    for ddl in (
        SQL_ALERTS,
        SQL_DLQ,
        SQL_INCIDENTS,
        SQL_INCIDENT_RECLUSTER_AUDITS,
        SQL_INCIDENT_STEPS,
        SQL_DIAGNOSIS_RUNS,
        SQL_EVIDENCE,
        SQL_REMEDIATION_ACTIONS,
        SQL_KNOWLEDGE_DOCS,
        SQL_KNOWLEDGE_CHUNKS,
        SQL_LOOP_AUDITS,
        SQL_RUNTIME_CONFIGS,
        SQL_LLM_PROVIDERS,
        SQL_USERS,
        SQL_EMAIL_CODES,
        SQL_INTEGRATIONS,
        SQL_TASKS,
        SQL_TASK_TARGETS,
        SQL_SCHEMA_MIGRATIONS,
    ):
        await conn.execute_script(ddl)

    # 历史迁移走基线登记：老库首次启动跑一遍，之后跳过。
    if settings.database_url.startswith("mysql"):
        if await _migration_applied(conn, BASELINE_VERSION):
            logger.debug("迁移基线 %s 已应用，跳过历史 ALTER", BASELINE_VERSION)
        else:
            await _run_baseline_migrations(conn)
            await _mark_migration(conn, BASELINE_VERSION)
            logger.info("迁移基线 %s 已应用", BASELINE_VERSION)

    # 邮箱唯一索引与基线无关：新库 DDL 已带，老库在这里补（幂等）。
    await _ensure_unique_user_email(conn)

    # 分级自动处置（F-06 扩展）：执行回执字段补列。
    # 与基线无关——老库（含各测试库）的 baseline 已登记，放在基线块里永远不会执行；
    # 这里每次启动幂等补列，新库 DDL 已带则 no-op。
    for _col, _def in (
        ("exec_status", "VARCHAR(16) NULL"),
        ("exec_exit_code", "INT NULL"),
        ("exec_output", "TEXT NULL"),
        ("exec_duration_ms", "INT NULL"),
        ("executed_at", "DATETIME NULL"),
    ):
        await _exec_idempotent(
            conn, f"ALTER TABLE remediation_actions ADD COLUMN {_col} {_def}"
        )

    # 种子与基线无关：每次启动都跑，只补缺号（幂等）。
    await _seed_llm_providers()


async def _run_baseline_migrations(conn) -> None:
    """2026-10 基线：把 F-01 时期的骨架表升级到当前结构。

    整块只在 `schema_migrations` 里没有 `BASELINE_VERSION` 时执行一次。
    内部仍保留 `_exec_idempotent`：老库可能已经在更早版本跑过其中一部分。
    """
    # F-09：旧骨架表（id/name/role/enabled）→ 全量表（含 enabled→status）
    await _migrate_llm_providers(conn)
    await _migrate_users(conn)
    # F-12：告警显式携带来源目标，作为任务归属推导的唯一依据（30-F-12 §5.3）
    await _exec_idempotent(
        conn, "ALTER TABLE alerts ADD COLUMN integration_id INT NULL"
    )
    await _exec_idempotent(
        conn, "CREATE INDEX idx_alerts_integration ON alerts (integration_id)"
    )
    # 去重命中计数：原先只刷新 last_seen_at，"被去重了多少次"无处可查，
    # 前端「去重」列只能造假。补一列真实计数（含首次入库，故 DEFAULT 1）。
    await _exec_idempotent(
        conn,
        "ALTER TABLE alerts ADD COLUMN dedup_count INT NOT NULL DEFAULT 1",
    )

    # 兼容已经由 F-01 M1 创建的旧版 incidents 表。
    columns = {
        "fingerprint": "CHAR(40) NULL",
        "component": "VARCHAR(64) NOT NULL DEFAULT ''",
        "last_seen_at": "DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP",
        "root_cause": "VARCHAR(255) NULL",
        "root_cause_confidence": "FLOAT NULL",
        "root_cause_source": "VARCHAR(16) NULL",
        "actual_cause": "VARCHAR(255) NULL",
        "resolved_at": "DATETIME NULL",
        "confirmed_by": "INT NULL",
    }
    for name, definition in columns.items():
        await _exec_idempotent(conn, f"ALTER TABLE incidents ADD COLUMN {name} {definition}")
    # F-03 V1.1：incident_steps 在 run_id 引入前就存在的安装需要补列。
    await _exec_idempotent(
        conn,
        "ALTER TABLE incident_steps ADD COLUMN run_id VARCHAR(32) NOT NULL DEFAULT ''",
    )
    await _exec_idempotent(conn, "CREATE INDEX idx_run ON incident_steps (run_id)")
    for ddl in (
        "CREATE INDEX idx_comp_status_win ON incidents (component,status,last_seen_at)",
        "CREATE INDEX idx_created ON incidents (created_at)",
        "CREATE INDEX idx_fingerprint ON incidents (fingerprint)",
    ):
        await _exec_idempotent(conn, ddl)
    await _exec_idempotent(
        conn, "UPDATE incidents SET status='new' WHERE status='open'"
    )
    # F-05：知识库表在 content/content_hash/component 引入前就存在的安装需要补列。
    # CREATE TABLE IF NOT EXISTS 不会给已存在的表加字段。
    for table, columns in {
        "knowledge_docs": {
            "content": "MEDIUMTEXT NOT NULL",
            "content_hash": "CHAR(40) NULL",
            # F-05 V1.1 B3：文档级组件标签，供 GET /knowledge 列表直接展示与
            # 粗筛，免去按切片众数聚合（N+1 查询）。
            "component": "VARCHAR(64) NOT NULL DEFAULT 'common'",
            "updated_at": "DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP",
        },
        "knowledge_chunks": {
            "embedding_id": "VARCHAR(64) NULL",
            "is_latest": "TINYINT(1) NOT NULL DEFAULT 1",
        },
    }.items():
        for name, definition in columns.items():
            await _exec_idempotent(
                conn, f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
            )
    await _exec_idempotent(
        conn, "ALTER TABLE knowledge_docs MODIFY COLUMN content MEDIUMTEXT NOT NULL"
    )
    # 回填历史数据（component 为空的旧事件）。这类**数据修复**不属于
    # 幂等 DDL：失败必须暴露，不能再被吞掉。
    from app.models import Alert, Incident
    from app.incidents.cluster import event_fingerprint

    for incident in await Incident.filter(component=""):
        first_alert = await Alert.filter(incident_id=incident.id).order_by("created_at").first()
        if first_alert is None:
            continue
        latest = await Alert.filter(incident_id=incident.id).order_by("-last_seen_at").first()
        await Incident.filter(id=incident.id).update(
            component=first_alert.component,
            fingerprint=event_fingerprint(first_alert.component, incident.title),
            last_seen_at=latest.last_seen_at if latest else first_alert.last_seen_at,
        )


async def close_db() -> None:
    """关闭 Tortoise 连接（容错）。

    `Tortoise.close_connections()` 会逐个关闭 aiomysql 连接的
    transport；若该连接所属的事件循环**已经被关闭**（典型场景：
    `TestClient` 退出时销毁了自己的 portal 循环，而连接池还挂在上面），
    `transport.close()` 里的 `loop.call_soon(...)` 会抛
    `RuntimeError: Event loop is closed`，让调用方的 teardown 整个失败。

    这种"循环先死了、连接后收"的时序在测试里很常见，且此时连接的
    socket 已随循环一起释放，没有资源泄漏 —— 因此直接吞掉该异常，
    不让一个收尾动作把整个测试模块判为 ERROR。
    """
    try:
        await Tortoise.close_connections()
    except RuntimeError as exc:  # pragma: no cover - 取决于事件循环时序
        if "Event loop is closed" not in str(exc):
            raise


async def _reset_connections() -> None:
    """丢弃 Tortoise 的连接注册表，让下一次 init_db() 从零建立连接。

    用途：跨事件循环复用 ORM 的场景（测试里很常见）。当上一个
    `TestClient` 的 portal 循环已关闭、而 aiomysql 连接池仍注册在
    Tortoise 内部时，新循环里第一次查询会去操作挂在死循环上的旧连接，
    抛 `RuntimeError: Event loop is closed`。

    这里直接清空 `Tortoise._connections`（不尝试优雅关闭 —— 优雅关闭
    正是会失败的那一步），随后 `init_db()` 会重新建池。
    """
    try:
        Tortoise._connections.clear()  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - 内部结构变动时的兜底
        pass
