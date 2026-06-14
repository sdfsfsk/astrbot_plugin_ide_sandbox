# astrbot_plugin_ide_sandbox

IDE 管理是一个 AstrBot 插件，为 LLM 提供隔离沙盒里的文件管理、代码编辑、命令执行、任务记录和 WebUI 管理能力。它适合让机器人在群聊或私聊里辅助写脚本、查看项目文件、运行受限命令，并让管理员在 AstrBot Dashboard 里实时查看沙盒状态。

## 功能

- 群聊和私聊独立沙盒，文件默认限制在 `data/astrbot_plugin_ide_sandbox/sandboxes/{sandbox_id}/`。
- LLM 工具：读写文件、追加/编辑文件、搜索文本、列目录、删除文件、打包下载、执行命令、运行测试、GitHub 克隆、待办事项。
- WebUI 页面：总览全部沙盒、查看文件树、编辑文本文件、查看工具活动、查看任务和待办。
- 权限分级：主人、沙盒管理员、文件管理员、命令管理员、群管理员、普通成员。
- 安全限制：命令白名单、危险命令黑名单、敏感文件保护、单次写入限制、文件大小限制、命令超时和输出截断。
- 群文件互通：可列出、下载和上传群文件，适合把群友发来的项目文件放进沙盒处理。

## 安装

把仓库放到 AstrBot 的插件目录：

```bash
cd AstrBot/data/plugins
git clone https://github.com/<your-name>/astrbot_plugin_ide_sandbox.git
```

安装依赖：

```bash
pip install -r AstrBot/data/plugins/astrbot_plugin_ide_sandbox/requirements.txt
```

然后重启 AstrBot，并在插件配置里设置主人 QQ、管理员和需要开启的能力。

## 常用命令

| 命令 | 说明 |
| --- | --- |
| `ide` / `ide帮助` | 查看插件帮助和当前沙盒状态 |
| `ide列表` / `沙盒列表` | 列出当前沙盒文件 |
| `ide清空` / `清空沙盒` | 预览清空当前沙盒，确认后才删除 |
| `ide权限` | 查看自己的沙盒权限 |
| `ide添加管理员` | 添加沙盒管理员 |
| `ide删除管理员` | 删除沙盒管理员 |

## WebUI

插件提供 `ide-dashboard` 页面入口。开启方式：

1. 在插件配置中启用 `ide_sandbox_webui_enabled`。
2. 在 `ide_sandbox_webui_allowed_users` 填入允许访问的 AstrBot Dashboard 用户名。
3. 回到 AstrBot 插件卡片，点击打开插件 UI 页面。

WebUI 可以查看全部沙盒总览、当前沙盒文件、工具活动记录、任务记录和待办事项。默认不会向未授权 Dashboard 用户开放。

## 权限与安全

重要配置项：

| 配置 | 说明 |
| --- | --- |
| `master_qq` | 机器人主人 QQ，拥有最高权限 |
| `ide_sandbox_admins` | 沙盒管理员，拥有完整文件和命令权限 |
| `ide_sandbox_terminal_admins` | 文件管理员，仅文件操作最高权限 |
| `ide_sandbox_cmd_admins` | 命令管理员，可绕过命令白名单，但仍受基础黑名单保护 |
| `ide_sandbox_allow_members` | 是否允许普通群成员使用 AI IDE 沙盒工具 |
| `ide_sandbox_allow_execution` | 是否允许 LLM 执行命令 |
| `ide_sandbox_allow_test` | 是否允许 LLM 运行 pytest/unittest |
| `ide_sandbox_allow_git_clone` | 是否允许 LLM 克隆 GitHub 仓库 |
| `ide_sandbox_cover_only_mode` | 仅翻唱联动模式，会关闭写入、删除、命令执行等高风险能力 |
| `ide_sandbox_admins_can_bypass` | 高危开关，允许管理员绕过沙盒路径和命令白名单限制 |

建议先保持命令执行、测试运行、Git 克隆和管理员绕过关闭，只在可信群聊中按需开启。

## 开发检查

发布包默认不包含内部测试目录。修改源码后可以先做语法检查：

```bash
python -m py_compile base.py command_tools.py events.py file_tools.py git_tools.py group_files.py main.py security.py tool_models.py web_api.py workflow_tools.py
```

## 版本

当前版本：`1.5.1`

## License

MIT
