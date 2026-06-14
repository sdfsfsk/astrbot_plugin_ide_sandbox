Kimi 风格整表读取或设置待办事项列表。

使用场景：
- 复杂任务开始前一次性设置完整步骤。
- 执行中把某项状态改为 in_progress 或 done。
- 不传 todos 时读取当前待办列表。

Args:
    todos(string, optional): JSON 数组，例如
        `[{"title":"读取项目结构","status":"done"},{"title":"补测试","status":"in_progress"}]`
        status 只能是 pending、in_progress、done。

Returns:
    更新后的待办列表。
