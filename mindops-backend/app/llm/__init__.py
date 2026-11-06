"""F-09 用户自接大模型接入层（LLM Gateway）。

- vault.py    密钥保险库（Fernet 加解密 + 脱敏，PRD §9.4）
- registry.py 厂商预设注册表（7 家 OpenAI 兼容预设）
- gateway.py  统一调用入口（保存即测试 / 真实调用 / 桩兜底）

智能体代码只经 model_router → gateway 消费模型，不感知厂商（§5.1）。
"""
