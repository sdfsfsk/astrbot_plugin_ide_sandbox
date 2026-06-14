from __future__ import annotations

import functools
from typing import Literal, TypeVar

from pydantic import BaseModel, Field, ValidationError, model_validator
from astrbot.core.platform.astr_message_event import AstrMessageEvent


ModelT = TypeVar("ModelT", bound=BaseModel)


def validate_with(model_cls: type[ModelT]):
    """在 LLM 工具调用时，用 Pydantic 模型校验 kwargs 参数。"""
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(self, event: AstrMessageEvent, **kwargs):
            try:
                validated = model_cls(**kwargs)
            except ValidationError as e:
                errors = "\\n".join(
                    f"- {'.'.join(str(x) for x in err['loc'])}: {err['msg']}"
                    for err in e.errors()
                )
                return f"⛔ 参数校验失败，请修正后重试：\\n{errors}"
            return await func(self, event, **validated.model_dump())
        return wrapper
    return decorator


class IdeListTreeArgs(BaseModel):
    """ide_list_tree 的参数校验模型。"""
    root: str = Field(default='', description='要查看的相对目录，留空表示沙盒根目录。')
    max_depth: int = Field(default=3, description='最大递归深度，范围 1-8，默认 3。', ge=1, le=8)
    max_entries: int = Field(default=200, description='最多展示条目数，范围 20-1000，默认 200。', ge=20, le=1000)


class IdeFileInfoArgs(BaseModel):
    """ide_file_info 的参数校验模型。"""
    path_name: str = Field(..., description='文件或目录的相对路径，超级管理员可传绝对路径。', min_length=1)


class IdeReadFileRangeArgs(BaseModel):
    """ide_read_file_range 的参数校验模型。"""
    filename: str = Field(..., description='文件相对路径，超级管理员可传绝对路径。', min_length=1)
    start_line: int = Field(default=1, description='起始行号，从 1 开始。', ge=1, le=100000)
    end_line: int = Field(default=120, description='结束行号，最多读取 400 行。', ge=1, le=100000)


class IdeSearchTextArgs(BaseModel):
    """ide_search_text 的参数校验模型。"""
    query: str = Field(..., description='要搜索的文本或正则表达式。', min_length=1)
    root: str = Field(default='', description='搜索目录或文件，留空表示沙盒根目录。')
    filename_pattern: str = Field(default='', description='文件名过滤，如 *.py 或 *.json。')
    regex: bool = Field(default=False, description='是否按正则搜索，默认 False。')
    case_sensitive: bool = Field(default=False, description='是否区分大小写，默认 False。')
    max_results: int = Field(default=50, description='最大结果数，范围 1-200，默认 50。', ge=1, le=200)
    output_mode: Literal['content', 'files_with_matches', 'count_matches'] = Field(
        default='content',
        description="输出模式：content=匹配行，files_with_matches=文件列表，count_matches=每文件匹配数。",
    )
    head_limit: int = Field(default=250, description='分页返回条数，0 表示不额外限制。', ge=0, le=1000)
    offset: int = Field(default=0, description='跳过前 N 条结果后再返回。', ge=0, le=100000)
    include_ignored: bool = Field(default=False, description='是否包含通常被忽略的目录/文件。敏感文件仍会过滤。')


class IdeReadFileArgs(BaseModel):
    """ide_read_file 的参数校验模型。"""
    filename: str = Field(..., description='要读取的文件名（不含路径），或超级管理员使用的绝对路径。', min_length=1)
    line_offset: int = Field(default=1, description='起始行号，从 1 开始；负值表示从末尾倒数。默认 1。', ge=-10000, le=10000)
    n_lines: int = Field(default=1000, description='要读取的行数，默认最多 1000 行。', ge=1, le=1000)

    @model_validator(mode="after")
    def _validate_line_offset(self):
        if self.line_offset == 0:
            raise ValueError("line_offset 不能为 0；读取首行请用 1，读取末尾请用负数。")
        return self


class IdeWriteFileArgs(BaseModel):
    """ide_write_file 的参数校验模型。"""
    filename: str = Field(..., description='文件名（不含路径），或超级管理员使用的绝对路径。', min_length=1)
    content: str = Field(..., description='要写入的完整内容。建议单文件脚本直接一次写入，大型项目拆分模块。', min_length=1)
    dry_run: bool = Field(default=False, description='为 true 时只返回预览，不实际写入。')


class IdeAppendToFileArgs(BaseModel):
    """ide_append_to_file 的参数校验模型。"""
    filename: str = Field(..., description='文件名（不含路径），或超级管理员使用的绝对路径。', min_length=1)
    content: str = Field(..., description='要追加到文件末尾的内容。', min_length=1)


class IdeEditFileArgs(BaseModel):
    """ide_edit_file 的参数校验模型。"""
    filename: str = Field(..., description='要编辑的文件名，或超级管理员使用的绝对路径。', min_length=1)
    old_string: str = Field(default='', description='单条编辑时要替换的旧字符串。')
    new_string: str = Field(default='', description='单条编辑时用于替换的新字符串。')
    replace_all: bool = Field(default=False, description='是否替换全部匹配；默认 False，只替换第一处，和 Kimi CLI 对齐。')
    edits: str = Field(default='', description='批量编辑的 JSON 列表，支持 replace_all 字段。传入 edits 时，old_string/new_string 参数会被忽略。')
    dry_run: bool = Field(default=False, description='为 true 时只返回预览，不实际写入。')


class IdeDeleteFileArgs(BaseModel):
    """ide_delete_file 的参数校验模型。"""
    filename: str = Field(..., description='要删除的文件名，或超级管理员使用的绝对路径。', min_length=1)
    dry_run: bool = Field(default=False, description='为 true 时只预览删除目标，不实际删除。')


class IdeClearSandboxArgs(BaseModel):
    """ide_clear_sandbox 的参数校验模型。"""
    confirm: bool = Field(default=False, description='必须设为 true 才会清空当前沙盒，防止误操作。')
    dry_run: bool = Field(default=False, description='为 true 时只预览清空目标，不实际删除。')


class IdeExecuteArgs(BaseModel):
    """ide_execute 的参数校验模型。"""
    command: str = Field(..., description='要执行的命令字符串。', min_length=1)
    run_in_background: bool = Field(default=False, description='是否以后台任务运行，默认 False。 设为 True 时不会等待命令结束，而是返回任务 ID，随后可用 ide_task_output 查询输出。')
    description: str = Field(default='', description='后台任务描述，run_in_background=true 时必填。')
    dry_run: bool = Field(default=False, description='为 true 时只返回命令预览和安全检查结果，不实际执行。')

    @model_validator(mode="after")
    def _validate_background_fields(self):
        if self.run_in_background and not self.description.strip():
            raise ValueError("run_in_background=true 时必须提供 description。")
        return self


class IdeTaskOutputArgs(BaseModel):
    """ide_task_output 的参数校验模型。"""
    task_id: str = Field(..., description='ide_execute 返回的任务 ID。', min_length=1)
    block: bool = Field(default=False, description='是否等待任务结束后再返回。')
    timeout: int = Field(default=30, description='block=true 时最多等待秒数，0 表示立即返回。', ge=0, le=3600)


class IdeTaskListArgs(BaseModel):
    """ide_task_list 的参数校验模型。"""
    active_only: bool = Field(default=True, description='是否只列出仍在运行的后台任务。')
    limit: int = Field(default=20, description='最多返回任务数。', ge=1, le=100)


class IdeTaskStopArgs(BaseModel):
    """ide_task_stop 的参数校验模型。"""
    task_id: str = Field(..., description='ide_execute 返回的任务 ID。', min_length=1)
    reason: str = Field(default='Stopped by ide_task_stop', description='停止任务的简短原因。')


class IdeExecuteElevatedArgs(BaseModel):
    """ide_execute_elevated 的参数校验模型。"""
    command: str = Field(..., description='要执行的命令字符串。', min_length=1)


class IdeRunTestArgs(BaseModel):
    """ide_run_test 的参数校验模型。"""
    test_path: str = Field(default='', description='测试文件或目录的路径（相对于沙盒根目录），留空则自动查找测试。')
    test_framework: Literal['pytest', 'unittest'] = Field(default='pytest', description="测试框架，可选 'pytest' 或 'unittest'，默认 'pytest'。")


class IdeGitCloneArgs(BaseModel):
    """ide_git_clone 的参数校验模型。"""
    repo_url: str = Field(..., description='GitHub 仓库地址，如 https://github.com/user/repo.git 或 https://github.com/user/repo', min_length=1)
    branch: str = Field(default='', description='要克隆的分支名，留空则使用默认分支。')


class IdeGlobArgs(BaseModel):
    """ide_glob 的参数校验模型。"""
    pattern: str = Field(..., description='Glob 模式，如 *.py 或 src/**/*.ts。', min_length=1)
    directory: str = Field(default='', description='搜索目录，留空表示沙盒根目录。')
    include_dirs: bool = Field(default=True, description='是否在结果中包含目录。')
    max_matches: int = Field(default=1000, description='最多返回匹配数。', ge=1, le=1000)


class IdeDownloadGroupFileArgs(BaseModel):
    """ide_download_group_file 的参数校验模型。"""
    filename: str = Field(..., description='群文件中的文件名。', min_length=1)


class IdeUploadToGroupArgs(BaseModel):
    """ide_upload_to_group 的参数校验模型。"""
    filename: str = Field(..., description='沙盒中要上传的文件名。', min_length=1)


class IdeThinkArgs(BaseModel):
    """ide_think 的参数校验模型。"""
    thought: str = Field(..., description='要记录的思考内容。', min_length=1)


class IdeAskUserArgs(BaseModel):
    """ide_ask_user 的参数校验模型。"""
    question: str = Field(..., description='要向用户提出的问题。', min_length=1)


class IdeListFileChangesArgs(BaseModel):
    """ide_list_file_changes 的参数校验模型。"""
    limit: int = Field(default=20, description='最多展示最近多少条变更，默认 20。', ge=1, le=100)


class IdeAddTodoArgs(BaseModel):
    """ide_add_todo 的参数校验模型。"""
    content: str = Field(..., description='待办事项的内容描述。', min_length=1)


class IdeCompleteTodoArgs(BaseModel):
    """ide_complete_todo 的参数校验模型。"""
    todo_id: int = Field(default=0, description='要完成的待办事项 ID（优先使用）。')
    content_keyword: str = Field(default='', description='如果不记得 ID，可以输入内容关键词来匹配。')


class IdeDeleteTodoArgs(BaseModel):
    """ide_delete_todo 的参数校验模型。"""
    todo_id: int = Field(default=0, description='要删除的待办事项 ID（优先使用）。')
    content_keyword: str = Field(default='', description='内容关键词匹配。')


class IdePackAndDownloadArgs(BaseModel):
    """ide_pack_and_download 的参数校验模型。"""
    dir_name: str = Field(default='', description='要打包的目录名（不含路径），或超级管理员使用的绝对路径。留空则打包整个沙盒。')
    zip_name: str = Field(default='sandbox_export.zip', description='导出的压缩包文件名，默认 sandbox_export.zip。')


class IdeClearTodosArgs(BaseModel):
    """ide_clear_todos 的参数校验模型。"""
    confirm: bool = Field(default=False, description='必须设为 true 才会清空，防止误操作。')


class IdeSetTodoListArgs(BaseModel):
    """ide_set_todo_list 的参数校验模型。"""
    todos: str = Field(
        default='',
        description='JSON 数组，元素格式 {"title":"任务","status":"pending|in_progress|done"}；留空则读取当前列表。',
    )
