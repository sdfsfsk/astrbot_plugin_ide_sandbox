列出当前用户可见的后台任务。

使用场景：
- ide_execute(run_in_background=true) 后查看还在运行的任务。
- 找回 task_id，再用 ide_task_output 查询输出或 ide_task_stop 停止任务。

权限规则：
- 任务创建者可以查看自己的任务。
- 主人、沙盒管理员、CMD 管理员可以查看可管理任务。
- 普通成员不能查看或停止别人启动的后台任务。

Args:
    active_only(bool, optional): 是否只列出运行中的任务，默认 true。
    limit(number, optional): 最多返回任务数，默认 20。

Returns:
    后台任务列表，包含 task_id、状态、描述、创建者和返回码。
