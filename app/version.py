"""版本唯一事实源。

运行时的 ``Settings.version``、FastAPI 的 ``version``、``/api/app/ping``
以及打包脚本生成的 Windows 版本资源都从这里取，不要再写死版本号。
"""
from __future__ import annotations

__version__ = "0.2.0"

#: 构建时间，由打包脚本（packaging/build.ps1）通过环境变量注入；开发态为空
BUILD_TIME = ""

__all__ = ["__version__", "BUILD_TIME"]
