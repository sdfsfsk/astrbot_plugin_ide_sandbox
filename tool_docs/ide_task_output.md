查询后台命令任务的状态和输出预览。

使用场景：
- ide_execute(run_in_background=true) 启动任务后查看进度。
- 任务结束后读取返回码和尾部日志。
- 需要完整日志时，根据返回的完整日志路径再用 ide_read_file 分页读取。

Args:
    task_id(string): 后台任务 ID。
    block(bool, optional): 是否等待任务结束后再返回，默认 false。
    timeout(number, optional): block=true 时最多等待秒数，默认 30。

Returns:
    任务状态、返回码、日志路径、尾部输出预览。
