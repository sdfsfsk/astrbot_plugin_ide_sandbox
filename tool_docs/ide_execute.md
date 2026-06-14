在沙盒环境中执行一条 shell 命令。

使用场景：
- 运行脚本、测试代码、查看构建结果。
- 长耗时命令可设置 run_in_background=true 后台运行。
- 后台任务启动后使用 ide_task_list / ide_task_output / ide_task_stop 管理。

安全规则：
- 命令执行必须开启 ide_sandbox_allow_execution。
- 普通成员即使 allow_members=true，也不能使用命令工具。
- CMD 管理员可绕过白名单，但仍受危险命令黑名单限制。
- 后台任务必须填写 description，方便后续识别。
- 命令进程 stdin 会关闭，避免交互式提示无限等待。
- dry_run=true 时只做安全检查和预览，不实际执行。

Args:
    command(string): 要执行的命令字符串。
    run_in_background(bool, optional): 是否以后台任务运行，默认 false。
    description(string, optional): 后台任务描述；run_in_background=true 时必填。
    dry_run(bool, optional): 为 true 时只预览，不执行。

Returns:
    前台命令返回 stdout/stderr/return code；后台命令返回 task_id 和日志路径。
