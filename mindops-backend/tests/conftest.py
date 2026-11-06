"""pytest 全局配置：固定测试环境变量 + 按模块绑定测试库。

背景（P0-4 修复后）：
- `Settings` 的安全相关默认值已改为生产安全值（`debug=False`、
  `jwt_secret=""`、`auth_allow_auto_register=False`）；
- 测试必须在 import 前通过环境变量把"开发/测试档"打开，否则生产档会因
  配置校验拒绝启动，且依赖「登录即注册」的历史用例（`test_f10.py`）会 401。

【测试库绑定 —— 为什么不能各模块自己 setdefault】
各测试模块顶部的写法是：

    os.environ.setdefault("MINDOPS_MYSQL_DB", "mindops_fXX_test")

但 pytest 会先 import 所有测试模块再执行用例，而 `os.environ` 是**进程级**的：
第一个被 import 的模块把库名写进去之后，其余模块的 `setdefault` 全部静默失效。
配合 `app.config.settings` 的 `@lru_cache` 单例，最终结果是"谁先被 import，
整轮测试就用谁的库" —— 后 import 的模块会把数据写进前一个模块的库，
出现诸如 `uk_base_scrape` 唯一键冲突（409 DUPLICATE_TARGET）这类难以定位的失败。

因此这里集中托管库名：由 conftest 在每个测试模块开始前，按模块名推导出
它自己的测试库并**强制赋值**（而不是 setdefault），从而让每个模块都跑在
独立、干净、互不污染的库上。
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from functools import lru_cache

import pytest

# ---------------------------------------------------------------------------
# 基础测试环境
# ---------------------------------------------------------------------------
_TEST_ENV = {
    # 开发/测试档：启动期只告警不拦截
    "MINDOPS_DEBUG": "true",
    # 测试库需要联调种子账号（sre_admin / sre001）；生产/普通 debug 启动默认不注入
    "MINDOPS_SEED_DEMO_USERS": "1",
    # 固定密钥，保证测试可重复（非生产用途）
    "MINDOPS_JWT_SECRET": "test-jwt-secret-for-pytest-only-not-for-production",
    "MINDOPS_MYSQL_PASSWORD": "123456",
    # 历史用例依赖「登录即注册」demo 行为
    "MINDOPS_AUTH_ALLOW_AUTO_REGISTER": "true",
    # 验证码通道走 mock：测试绝不能连真实 SMTP 发信（.env 里可能配着真实授权码）
    "MINDOPS_EMAIL_PROVIDER": "mock",
    "MINDOPS_CORS_ORIGINS": "http://localhost:3015",
    # 测试档强制走"无 Redis"路径（= 内存去重/限流 + 同步聚类）。
    # 原因不是"Redis 不可用"，而是 M2 模式改了对外契约：`alerts/service.py:194` 起，
    # Redis 在线时 webhook 只 publish 到 `alert.raw`、由订阅者异步聚类，响应里的
    # `incident_id` 变成 null —— 现有接口用例与前端都按同步返回写。2026-10-22 实测：
    # 一旦把 Redis 接上，`test_f04_api` 拿到 incident_id=None 且 TestClient 退出挂死。
    # Redis 分支本身由 tests/test_redis_backends.py 显式连库覆盖。
    "MINDOPS_REDIS_URL": "",
}

for _k, _v in _TEST_ENV.items():
    os.environ.setdefault(_k, _v)

# ---------------------------------------------------------------------------
# 测试产物隔离：Milvus Lite 文件 + 嵌入缓存
# ---------------------------------------------------------------------------
# `.env` 里 `MINDOPS_MILVUS_URI` 指向演示用的 E:/mindops-data/milvus_lite.db，
# 嵌入缓存默认落在 mindops-backend/data/embedding_cache.json。测试若照用，
# 会把测试文档写进演示向量库、并让缓存跨轮次污染（同一段文本命中旧向量，
# 换了模型也不知道）。这里强制改用本次运行专属的目录。
#
# 必须用 ASCII 路径：Milvus Lite 底层的 faiss 在 Windows 上无法处理中文路径
# （app/config.py 的启动校验也会告警）。E:/mindops-data 是项目约定的纯英文目录；
# 不存在时退回系统临时目录（此时 Milvus 用例可能跳过，但不会误写演示库）。
_ARTIFACT_BASE = "E:/mindops-data" if os.path.isdir("E:/mindops-data") else tempfile.gettempdir()
_TEST_ARTIFACT_DIR = os.path.join(_ARTIFACT_BASE, f"mindops-pytest-{os.getpid()}")
os.makedirs(_TEST_ARTIFACT_DIR, exist_ok=True)

# 强制覆盖（不是 setdefault）：调用者 shell 里残留的演示路径同样不能带进测试
os.environ["MINDOPS_MILVUS_URI"] = os.path.join(_TEST_ARTIFACT_DIR, "milvus_lite.db")
os.environ["MINDOPS_EMBED_CACHE_PATH"] = os.path.join(_TEST_ARTIFACT_DIR, "embedding_cache.json")


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    """整轮测试结束先停 Milvus Lite，再删除临时目录；否则 Windows 上文件被占用删不掉。"""
    milvus_path = os.path.join(_TEST_ARTIFACT_DIR, "milvus_lite.db")
    try:
        from milvus_lite.server_manager import server_manager_instance

        server_manager_instance.release_server(milvus_path)
    except Exception:
        pass
    shutil.rmtree(_TEST_ARTIFACT_DIR, ignore_errors=True)


# ---------------------------------------------------------------------------
# 屏蔽 mindops-backend/.env 中的"空值占位行"
# ---------------------------------------------------------------------------
# pydantic-settings 的优先级是「真实 os.environ > .env 文件」，且**逐字段独立判断**：
# 只要 env 中存在该键（哪怕是空串），就绝不会回落到 .env 的对应行。
# mindops-backend/.env 由 .env.example 生成，含 `MINDOPS_LLM_VAULT_KEY=`（空串），
# 会把测试动态写入的主密钥压成空，跨模块串跑时报 VAULT_LOCKED。
#
# 处理：收集 .env 中的空值键，在 pytest_configure 里显式写入 ""
# （空值一律视为"未配置"），既阻止 .env 反向压制，又保留测试后续直接赋值的能力。
from pathlib import Path as _Path  # noqa: E402

_EMPTY_KEYS: list[str] = []

_env_file = _Path(__file__).resolve().parent.parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _key, _, _val = _line.partition("=")
        _key, _val = _key.strip(), _val.strip()
        if _val == "":
            _EMPTY_KEYS.append(_key)
        # 非空值不灌进 os.environ：交给 pydantic-settings 自己从 .env 读取，
        # 避免把 `MINDOPS_MYSQL_DB=mindops` 写死而覆盖测试库。


def pytest_configure(config):  # noqa: ARG001
    for key in _EMPTY_KEYS:
        os.environ.setdefault(key, "")


# ---------------------------------------------------------------------------
# 按模块绑定测试库
# ---------------------------------------------------------------------------
# 各测试模块在文件顶部用 `os.environ.setdefault("MINDOPS_MYSQL_DB", "...")`
# 声明自己想要的库。conftest 在收集阶段把这些声明**抓取出来**做成映射，
# 再在每个模块开始前强制生效 —— 这样既保留各模块的原始意图，
# 又不依赖"谁先被 import"，彻底消除跨模块串库。
_DECL_RE = re.compile(
    r"""os\.environ(?:\.setdefault)?\s*[\('\[]\s*["']MINDOPS_MYSQL_DB["']\s*,\s*["']([\w\-]+)["']"""
)


@lru_cache(maxsize=None)
def _declared_db_for(module_file: str) -> str | None:
    """从测试模块源码里提取它声明的测试库名。"""
    try:
        src = _Path(module_file).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    m = _DECL_RE.search(src)
    return m.group(1) if m else None


def _reload_settings() -> None:
    """清缓存重建 `app.config.settings` 单例，并同步所有 `app.*` 模块的引用。

    `from app.config import settings` 会在每个模块命名空间留下**独立引用**。
    只重建 `app.config.settings` 是不够的：`app.db` / `app.auth.jwt` 等模块
    仍指向旧对象，库名变更对它们不可见 —— 这正是跨模块串跑时"数据写进别的
    模块的库"的直接原因。

    实现要点：先把**所有** app.* 模块当前持有的 settings 对象收集为"陈旧集合"，
    重建后逐个比对身份再替换。只改 `app.` 前缀的模块，避免误伤第三方库。
    """
    try:
        import app.config as cfg
    except Exception:  # pragma: no cover - app 尚未可导入时静默跳过
        return

    import sys as _sys

    # 1) 收集陈旧对象（可能不止一个：历史上有模块自行 cache_clear 过）
    stale: list[object] = [cfg.settings]
    app_mods = [
        m
        for m in _sys.modules.values()
        if m is not None and getattr(m, "__name__", "").startswith("app.")
    ]
    for mod in app_mods:
        holder = getattr(mod, "settings", None)
        if holder is not None and not any(holder is s for s in stale):
            stale.append(holder)

    # 2) 重建单例
    cfg.get_settings.cache_clear()
    fresh = cfg.get_settings()
    cfg.settings = fresh

    # 3) 把仍指向任一陈旧对象的模块引用改到新对象
    for mod in app_mods:
        if mod is cfg:
            continue
        if any(getattr(mod, "settings", None) is s for s in stale):
            setattr(mod, "settings", fresh)


def _bind_db(module) -> None:
    """把当前测试模块绑定到它自己声明的测试库。

    未声明库的模块（纯逻辑用例）保持当前 env 不变，避免把基线库改没。
    """
    name = getattr(module, "__name__", "").rsplit(".", 1)[-1]
    file = getattr(module, "__file__", None)

    db = _declared_db_for(file) if file else None
    if not db:
        # 回退：按模块名推导（如 test_f07 -> mindops_f07_test）
        base = name.replace("_api", "").replace("_integration", "")
        db = f"mindops_{base.removeprefix('test_')}_test"

    if os.environ.get("MINDOPS_MYSQL_DB") != db:
        os.environ["MINDOPS_MYSQL_DB"] = db
        _reload_settings()


def _flush_test_redis() -> None:
    """清空测试专用 Redis 库（db 9）。

    去重指纹是 Redis 里带 1 小时 TTL 的**全局**状态，而 `MemoryDedup` 随每个 app
    实例重置。所以"同一指纹再投一次应当算新"这类断言过去隐式依赖内存实现：Redis
    一旦真可达，上一条用例甚至上一轮运行留下的键就会把它吞成"重复投递"。

    连不上时静默跳过 —— 应用会降级到内存实现，测试语义与启用 Redis 前完全一致。
    """
    url = os.environ.get("MINDOPS_REDIS_URL", "")
    if not url:
        return
    try:
        import redis

        client = redis.Redis.from_url(url, socket_connect_timeout=1.5)
        client.flushdb()
        client.close()
    except Exception:
        return


@pytest.fixture(autouse=True)
def _redis_isolation():
    """逐条用例清库：用例之间不得通过 Redis 共享去重键或限流桶。"""
    _flush_test_redis()
    yield


def pytest_collection_modifyitems(config, items):  # noqa: ARG001
    """收集完成后：按命令行中模块的出现顺序，把库名基线设为第一个模块的库。

    即便测试模块在文件顶部就 import 了 `app.main`（触发 DB 初始化），
    也能在收集阶段先把基线定下来，减少第一条用例的错库窗口。
    """
    seen: list[str] = []
    for item in items:
        mod = getattr(item, "module", None)
        name = getattr(mod, "__name__", "") if mod is not None else ""
        name = name.rsplit(".", 1)[-1]
        if name and name not in seen:
            seen.append(name)

    if seen:
        # 以第一个模块的库作为进入 pytest 时的基线
        mod = next(
            (i.module for i in items if getattr(getattr(i, "module", None), "__name__", "").rsplit(".", 1)[-1] == seen[0]),
            None,
        )
        if mod is not None:
            _bind_db(mod)


@pytest.fixture(autouse=True, scope="module")
def _per_module_test_db(request):
    """每个测试模块开始前，把它绑定到自己的测试库。

    这是解决跨模块污染的关键：`conftest` 在每个 module 级 fixture
    建立时强制改写 `MINDOPS_MYSQL_DB` 并重建 settings 单例，
    使各模块的数据互不可见。
    """
    _bind_db(request.module)
    yield
