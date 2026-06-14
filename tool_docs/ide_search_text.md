在沙盒内搜索文本，优先调用系统 ripgrep（rg），回退到 Python 实现。

使用场景：
- 查找函数名、类名、变量名定义或引用位置。
- 定位报错信息、TODO、FIXME 等标记。
- 在修改文件前，先确认旧字符串在哪些文件中出现。

Tips:
- 总是优先使用本工具定位内容，而不是先读取整个大文件再手动查找。
- 搜索到结果后，可结合 ide_read_file 的 line_offset/n_lines 读取上下文。
- 使用 filename_pattern 限定文件类型可显著减少无关结果。

Args:
    query(string): 要搜索的文本或正则表达式。
    root(string, optional): 搜索目录或文件，留空表示沙盒根目录。
    filename_pattern(string, optional): 文件名过滤，如 *.py 或 *.json。
    regex(bool, optional): 是否按正则搜索，默认 False。
    case_sensitive(bool, optional): 是否区分大小写，默认 False。
    max_results(number, optional): 最大结果数，范围 1-200，默认 50。

Returns:
    匹配行列表，格式为 `相对路径:行号: 匹配内容`。
