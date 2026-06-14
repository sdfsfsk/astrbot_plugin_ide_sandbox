读取沙盒中指定文本文件的内容，支持分页和尾部读取。

使用场景：
- 查看已存在文件的局部内容。
- 根据 ide_search_text 的结果读取匹配行附近上下文。
- 读取长日志时使用负数 line_offset 查看尾部。

安全规则：
- 默认最多读取 1000 行 / 100KB，并自动截断超长行。
- line_offset 不能为 0；从第一行读取用 1，从末尾读取用负数。
- .env、key、token、secret、credentials 等敏感文件会被阻止读取。
- 图片、音频、视频、压缩包、可执行文件等非文本文件会被阻止按文本读取。

Args:
    filename(string): 文件相对路径，或超级管理员使用的绝对路径。
    line_offset(number, optional): 起始行号，从 1 开始；负值表示从末尾倒数。默认 1。
    n_lines(number, optional): 要读取的行数，范围 1-1000，默认 1000。

Returns:
    带行号的文本片段和继续读取提示。
