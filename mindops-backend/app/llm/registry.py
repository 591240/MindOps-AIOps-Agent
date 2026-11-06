"""厂商预设注册表（F-09 §5.2，`GET /llm/presets` 响应源）。

预设是前端表单辅助数据（选预设自动填 base_url 与模型下拉），不是运行时
白名单——`custom` 允许任意 OpenAI 兼容端点，运行时只信任 base_url+model_name。
新增厂商 = 本文件加一条预设，零改动内核（§5.1）。
"""

from __future__ import annotations

PRESETS: list[dict] = [
    # deepseek：模型名以 `/v1/models` 实测为准。旧名 deepseek-chat / deepseek-reasoner
    # 在当前 API 上已不可用（会 400 model_not_found），实测账号只提供下列两个。
    # 两个都会在 usage 里回 reasoning_tokens（推理型），思考计入 max_tokens 预算。
    # 【名称映射，勿再混淆】官方文档表格用「模型版本」列展示人读名，API 只认 id：
    #   deepseek-flash   ←→ 模型版本 DeepSeek-V4.1-Flash（第一槽位 chat，DB id=14 用这个）
    #   deepseek-v4-pro  ←→ 模型版本 DeepSeek-V4-Pro-0813
    # 传 "DeepSeek-V4.1-Flash" 会被拒：400 invalid_request_error
    #   "The supported API model names are deepseek-flash, deepseek-v4-pro"。人读名只作
    #   provider.name 展示用，别写进 model_name。
    {"provider_type": "deepseek", "label": "DeepSeek", "base_url": "https://api.deepseek.com/v1",
     "models": ["deepseek-flash", "deepseek-v4-pro"], "key_hint": "sk-",
     # 人读名映射：仅用于界面展示（/llm/presets → 前端），**绝不写进 model_name**。
     "model_labels": {"deepseek-flash": "DeepSeek-V4.1-Flash",
                      "deepseek-v4-pro": "DeepSeek-V4-Pro-0813"}},
    {"provider_type": "qwen", "label": "通义千问（DashScope）",
     "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
     "models": ["qwen3.8-flash", "qwen-flash", "qwen-plus", "qwen-max"], "key_hint": "sk-",
     "model_labels": {"qwen3.8-flash": "千问3.8-Flash"}},
    {"provider_type": "openai", "label": "OpenAI", "base_url": "https://api.openai.com/v1",
     "models": ["gpt-4o-mini", "gpt-4o"], "key_hint": "sk-"},
    # zhipu：`models[0]` 会被前端在「选预设」时自动填入 Model Name
    # （ModelSettingsView.tsx `model_name: preset.models[0]`），所以顺序即默认值。
    # 首位放 glm-4.5-air —— 它是覆盖该厂商免费/赠送资源包最广、额度最充足的型号，
    # 适合 chat 角色（Triage/Context/Remediate/Ticket 等高频步骤）。
    # glm-4.7 排在次位，适合 reasoning 角色（仅置信度不足回环的第 2 次诊断才走，低频）。
    {"provider_type": "zhipu", "label": "智谱 GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4",
     "models": ["glm-4.5-air", "glm-4.7", "glm-4.6v", "glm-4-flash", "glm-4-plus"], "key_hint": ""},
    {"provider_type": "moonshot", "label": "月之暗面 Kimi", "base_url": "https://api.moonshot.cn/v1",
     "models": ["moonshot-v1-8k"], "key_hint": "sk-"},
    {"provider_type": "ollama", "label": "Ollama（本地）", "base_url": "http://localhost:11434/v1",
     "models": ["qwen2.5:7b"], "key_hint": "可留空"},
    {"provider_type": "custom", "label": "自定义（OpenAI 兼容）", "base_url": "",
     "models": [], "key_hint": "如 vLLM / One-API / LM Studio"},
]

PROVIDER_TYPES = frozenset(preset["provider_type"] for preset in PRESETS)
# Key 可留空的厂商：本地端点 / 用户自建聚合网关
KEY_OPTIONAL_TYPES = frozenset({"ollama", "custom"})
# 自托管端点：允许指向 loopback / 私网（Ollama 默认 localhost，vLLM 常在内网）。
# 云元数据地址仍由 ssrf_guard 拒绝。
PRIVATE_ENDPOINT_TYPES = frozenset({"ollama", "custom"})


def allows_private_endpoint(provider_type: str | None) -> bool:
    return provider_type in PRIVATE_ENDPOINT_TYPES
