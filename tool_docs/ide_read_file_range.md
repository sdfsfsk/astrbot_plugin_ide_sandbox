按行号读取文件片段（兼容旧版，新代码建议直接用 ide_read_file 的 line_offset/n_lines）。

使用场景：
- 只需要查看某段代码，避免一次读取大文件。
- 根据 ide_search_text 的搜索结果定位到具体行号后，读取附近代码。

Args:
    filename(string): 文件相对路径，超级管理员可传绝对路径。
    start_line(number, optional): 起始行号，从 1 开始。
    end_line(number, optional): 结束行号，最多读取 400 行。

Returns:
    带行号的文件片段。
