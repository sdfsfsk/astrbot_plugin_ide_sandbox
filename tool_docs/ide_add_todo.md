添加一个待办事项到当前任务列表。
当 AI 接到复杂任务、需要分步骤执行时，应该先创建待办事项来跟踪进度。
完成后可以使用 ide_complete_todo 标记为已完成。
Args:
    content(string): 待办事项的内容描述。
Returns:
    添加结果，包含分配的待办 ID。
