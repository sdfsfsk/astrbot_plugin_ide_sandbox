在沙盒文件中进行精确字符串替换，支持单条或批量编辑。

使用场景：
- 修改已有文件的一小段内容，比完整重写更安全。
- 修 bug、改配置、重命名局部变量。
- 需要多处不同替换时，用 edits JSON 数组批量传入。

重要规则：
- old_string 必须精确匹配。
- 默认只替换第一处匹配，和 Kimi CLI 的 StrReplaceFile 对齐。
- 只有明确需要替换全部匹配时才设置 replace_all=true。
- 批量 edits 每项也可带 replace_all，默认 false。
- dry_run=true 时只返回 diff 预览，不实际写入。

Args:
    filename(string): 要编辑的文件名，或超级管理员使用的绝对路径。
    old_string(string, optional): 单条编辑时要替换的旧字符串。
    new_string(string, optional): 单条编辑时用于替换的新字符串。
    replace_all(bool, optional): 是否替换全部匹配，默认 false。
    edits(string, optional): 批量编辑 JSON 数组，例如
        `[{"old_string":"a","new_string":"b","replace_all":false}]`。
    dry_run(bool, optional): 为 true 时只预览，不写入。

Returns:
    替换结果说明，包含替换次数。
