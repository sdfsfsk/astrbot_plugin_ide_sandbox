ide_clear_sandbox：清空当前聊天或用户对应沙盒中的所有文件和目录。

使用场景：
- 用户明确要求“清空沙盒”“删除沙盒内所有文件”“重新开始”。
- 必须先向用户确认；用户确认后调用 confirm=true。
- 不要使用 ide_execute 执行 rm、del、rmdir 或 Remove-Item 来清空沙盒，请使用本工具。

安全规则：
- 只删除当前沙盒根目录下的内容，不删除沙盒目录本身。
- 需要文件操作权限，不需要命令执行权限。
- confirm=false 或 dry_run=true 只预览，不实际删除。

Args:
    confirm(bool): 必须为 true 才会实际清空。
    dry_run(bool): 为 true 时只预览清空目标。

Returns:
    清空结果说明。
