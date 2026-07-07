---
paths: "scripts/utils/db_manager.py|scripts/utils/data_provider.py|scripts/utils/tushare_client.py"
---
# 🗄️ 数据库与数据源规范

## DB 访问
- 始终使用 `db_manager.py` 封装的 API（`get_connection`, `query_to_df` 等）
- 禁止裸 SQL 字符串拼接（防注入）
- 写操作需通过 DB API 方法，不要直接 `cursor.execute()`

## 多数据源
- 首选 Tushare Pro（通过 `tushare_client.py`）
- Tushare 不可用时自动 fallback 到 AkShare（通过 `data_provider.py`）
- mootdx 作为离线备用数据源

## 连接管理
- 使用 `with` 语句管理连接
- 不在循环中重复建立连接
- 长查询设置超时
