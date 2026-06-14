在沙盒中运行测试。
当 AI 需要验证代码正确性、运行单元测试时使用此工具。
支持 pytest 和 unittest 两种框架。
注意：此功能需要管理员在插件配置中开启 ide_sandbox_allow_test。
Args:
    test_path(string, optional): 测试文件或目录的路径（相对于沙盒根目录），留空则自动查找测试。
    test_framework(string, optional): 测试框架，可选 'pytest' 或 'unittest'，默认 'pytest'。
Returns:
    测试结果摘要（通过/失败数量、错误信息）。
