按 Glob 模式查找沙盒内文件或目录。

使用场景：
- 快速查找 `*.py`、`src/**/*.ts`、`**/package.json` 等路径。
- 不需要读取文件内容，只需要找到文件名或目录。

安全规则：
- pattern 不能以 `**` 开头，避免无边界递归扫描大目录。
- 敏感文件会从结果中隐藏。

Args:
    pattern(string): Glob 模式，如 `*.py` 或 `src/**/*.js`。
    directory(string, optional): 搜索目录，留空为沙盒根目录。
    include_dirs(bool, optional): 是否包含目录，默认 true。
    max_matches(number, optional): 最多返回匹配数量，默认 1000。

Returns:
    匹配路径列表。
