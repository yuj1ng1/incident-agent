# Incident Agent

SRE 故障排查 Agent 的初始化框架。

当前版本说明：**10.4 初始化agent loop框架**。

## 当前内容

- `main.py`：交互入口、模型调用和每轮最多 5 次迭代的 agent loop。
- `models/`：告警、证据和故障事件的数据模型。
- `prompts/`：故障排查提示词。
- `tools/`：工具注册、参数 schema 生成、工具执行，以及模拟日志和指标查询。
- `model_client/`：模型客户端模块的初始占位文件。

## 环境配置

依赖列在 `requirements.txt` 中。数据模型使用 Pydantic 2 的 `field_validator`。

将 `.env.example` 复制为本地 `.env`，填写 `DEEPSEEK_API_KEY`。真实 `.env`、编辑器设置、Python 缓存和虚拟环境均由 `.gitignore` 排除。

## 初始化版本的验证状态

10 个 Python 源文件通过语法检查。当前代码保留了初始化时的内容，尚未完成端到端运行验证。

- `tools/logs.py`、`tools/metrics.py` 和 `tools/tool_manager.py` 使用顶层 `tool_registry` 导入；从项目根目录按当前包结构加载时会找不到该模块。
- `main.py` 当前导入的 `agent_prompt` 是模块，后续需要检查传给模型的 `instructions` 值。
- 日志和指标查询返回模拟数据；模型接口、模型名称和真实故障排查流程尚未验证。

仓库中的依赖清单用于记录直接依赖，版本尚未全部锁定。
