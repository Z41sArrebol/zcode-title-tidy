# Changelog

## 0.2.0

公开发布准备版。

- 模型配置：API 地址、模型、API Key 可在设置中配置，支持 OpenAI Chat Completions 兼容接口和连接测试。
- 数据目录：方案、备份、配置、日志迁移到 `%USERPROFILE%\.zcode-title-tidy`，不再写入程序目录。
- ZCode 数据目录可配置；启动时检查数据库结构，不兼容时禁止写入。
- 新增 Windows 单文件 exe 和发布打包脚本。
- 撤销改为字段级：只恢复标题相关字段，保留之后的新会话和其他 `meta_json` 字段。
- 方案区分“生成时原标题”和“数据库当前标题”，过期方案会标记并跳过。
- 写入接口增加 CSRF token、Host/Origin/Content-Type 校验、参数校验和并发锁。
- ZCode 进程检测失败时按“不可写”处理；写后校验失败不再报告成功。
- 增加合成数据测试：`tests/test_safety.py`、`tests/test_config.py`。
