停止一个正在运行的后台命令任务。

使用场景：
- 长任务不再需要时手动停止。
- 任务运行异常时强制终止。

Args:
    task_id(string): ide_execute 返回的任务 ID。

Returns:
    停止结果。
