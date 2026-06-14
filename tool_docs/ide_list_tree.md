以目录树形式列出沙盒目录结构。

使用场景：
- 项目结构复杂，需要像 Codex 一样先理解目录层级。
- 定位某个模块、资源或配置文件所在位置。
- 文件数量过多，ide_list_files 展示不全时。

Args:
    root(string, optional): 要查看的相对目录，留空表示沙盒根目录。
    max_depth(number, optional): 最大递归深度，范围 1-8，默认 3。
    max_entries(number, optional): 最多展示条目数，范围 20-1000，默认 200。

Returns:
    目录树文本。
