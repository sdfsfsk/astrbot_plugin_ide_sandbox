将沙盒中的指定目录打包为 ZIP 并直接发送给用户。
当用户要求下载代码、获取生成的项目或文件时使用此工具。
超级管理员可传入绝对路径打包沙盒外目录。
Args:
    dir_name(string, optional): 要打包的目录名（不含路径），或超级管理员使用的绝对路径。留空则打包整个沙盒。
    zip_name(string, optional): 导出的压缩包文件名，默认 sandbox_export.zip。
Returns:
    打包并发送的结果说明。
