---
paths: "scripts/agent*.py"
---
# 📋 Agent 脚本编码规范

## D3 异常处理
- 每个数据源/步骤必须有三段式：触发条件 → 一线修复 → 兜底
- 异常必须用 try/except，不能静默 `pass`
- 异常信息必须包含上下文，如 `f"[fetch] {stock_code} 行情获取失败: {e}"`

## D4 CHECKPOINT
- 文件读取前检查存在性
- 数据计算前检查列名和数据长度
- 输出前检查报告完整性（文件已生成？不为空？）
- 关键决策前交叉验证数据源

## D9 反例
- 禁止静默吞异常 (except: pass)
- 禁止硬编码路径（用 settings.local.json 的 env / project root）
- 禁止使用 `node` 命令（环境中无 Node.js）
- 禁止直接调用 Tushare API（必须通过 `data_provider.py` / `tushare_client.py`）

## 工具函数
- 数据源访问：`scripts/utils/data_provider.py` 或 `tushare_client.py`
- DB 操作：`scripts/utils/db_manager.py` 的 API 方法
- 环境变量：从 `os.environ` 读取，在 settings.local.json 配置
