# 维护工具

`life-vision-check.py` 使用程序生成的红蓝图片检查当前 DeepSeek 模型的真实识图能力，会使用已配置密钥并占用一次每日模型额度，不发送 QQ 消息。结果写入 `logs/reports/qq-life-vision-check.json`。

在项目根目录运行：

```bash
.venv/bin/python scripts/maintenance/life-vision-check.py
```

启动、停止和管理 QQ 环境仍使用上一层的原有脚本。
