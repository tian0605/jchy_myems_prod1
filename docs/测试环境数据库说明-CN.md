# 测试环境数据库说明

## 环境用途

本环境用于本地功能验证，已导入生产快照数据。不得将本环境中的数据、账号信息或配置直接用于生产环境。

## MySQL 连接信息

| 项目 | 值 |
| --- | --- |
| 主机 | `127.0.0.1` |
| 端口 | `3306` |
| 配置文件 | `myems-api/.env` |
| 数据目录 | `.local/mysql/data` |
| 数据库版本 | `6.3.14` |

数据库用户名和密码仅保存在本机 `myems-api/.env`，本文件不记录任何凭据。

## 已恢复数据库

生产快照恢复后，本地 MySQL 包含以下 MyEMS 数据库：

- `myems_system_db`
- `myems_user_db`
- `myems_energy_db`
- `myems_energy_baseline_db`
- `myems_energy_model_db`
- `myems_energy_plan_db`
- `myems_energy_prediction_db`
- `myems_billing_db`
- `myems_carbon_db`
- `myems_historical_db`
- `myems_fdd_db`
- `myems_production_db`
- `myems_reporting_db`

## 数据快照与备份

| 项目 | 路径或值 |
| --- | --- |
| 导入快照 | `docs/prod-20260601-224218.tar.gz` |
| 快照内容 | `myems-prod-all-databases.sql.gz` |
| 导入前本地备份 | `.local/backups/20261007-223807` |
| 导入日期 | `2026-10-07` |

导入后已确认存在 8 个用户、7 个空间和 8 个仪表记录。

## AI 助手测试配置

- 用户端开发地址：`http://localhost:3000`
- AI 助手地址：`http://localhost:3000/ai-assistant`
- API 服务地址：`http://localhost:8000`
- 用户端菜单路由：`/ai-assistant`
- AI 菜单为一级菜单，并已授予平台管理员、企业管理员和企业操作员默认模板。

生产快照携带的 AI 模型配置已停用，因为其密文只能由生产环境加密主密钥解密。需要在管理端新建本地模型配置，并使用测试用 API Key。

网页保存的 API Key 使用服务器端 `AI_MODEL_CONFIG_ENCRYPTION_KEY` 加密后写入数据库，Key 本身不会在管理端页面或 API 查询响应中返回。该加密主密钥仅保存在本机 `myems-api/.env`。

## 维护注意事项

1. 恢复新的生产快照前，先备份现有 `myems_*` 数据库。
2. 不要把生产 API Key、数据库密码或加密主密钥提交到 Git、文档或聊天记录。
3. 导入快照后，检查 `myems_system_db.tbl_versions` 的物理 schema 是否已具备当前功能需要的表和列，再更新版本元数据。
4. 测试完成后，如不再需要生产快照数据，应恢复演示数据或删除本地测试实例。