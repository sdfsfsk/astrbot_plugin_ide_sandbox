以管理员权限（UAC 提权）执行一条命令。

使用场景：
- 仅用于主人处理极少数 Windows 维护操作，目前只允许 winget source 子命令。

⚠️ 使用前提：
1. 必须在 Windows 环境下运行 AstrBot。
2. AstrBot 有图形界面（远程桌面断开时 UAC 弹窗无法显示，会超时失败）。
3. 插件配置中开启了 ide_sandbox_allow_elevated。
4. 执行时会弹出 Windows UAC 对话框，需要有人在电脑前点击"是"。

Args:
    command(string): 要执行的命令字符串。

Returns:
    命令的标准输出和标准错误。
