从 GitHub 拉取远程仓库到当前沙盒。
当 AI 需要获取开源项目代码进行分析、修改或参考时使用此工具。
注意：此功能需要管理员在插件配置中开启 ide_sandbox_allow_git_clone。
Args:
    repo_url(string): GitHub 仓库地址，如 https://github.com/user/repo.git 或 https://github.com/user/repo
    branch(string, optional): 要克隆的分支名，留空则使用默认分支。
Returns:
    克隆结果说明，包含仓库目录名。
