@echo off
chcp 65001 >nul
echo ========================================
echo   frp-lite GUI 一键打包脚本
echo ========================================
echo 注意: 使用 --windowed 参数，不会有控制台黑框

echo.
echo [1/4] 打包 frps_lite_gui.exe ...
pyinstaller --onefile --clean --windowed --name frps_lite_gui frps_lite_gui.py
if %ERRORLEVEL% neq 0 (
    echo [ERROR] frps_lite_gui 打包失败!
    pause
    exit /b 1
)
echo [OK] frps_lite_gui.exe 打包完成
echo.

echo [2/4] 打包 frpc_lite_gui.exe ...
pyinstaller --onefile --clean --windowed --name frpc_lite_gui frpc_lite_gui.py
if %ERRORLEVEL% neq 0 (
    echo [ERROR] frpc_lite_gui 打包失败!
    pause
    exit /b 1
)
echo [OK] frpc_lite_gui.exe 打包完成
echo.

echo [3/4] 打包 frps_service_manager.exe ...
pyinstaller --onefile --clean --windowed --name frps_service_manager frps_service_manager.py
if %ERRORLEVEL% neq 0 (
    echo [ERROR] frps_service_manager 打包失败!
    pause
    exit /b 1
)
echo [OK] frps_service_manager.exe 打包完成
echo.

echo [4/4] 打包 frpc_service_manager.exe ...
pyinstaller --onefile --clean --windowed --name frpc_service_manager frpc_service_manager.py
if %ERRORLEVEL% neq 0 (
    echo [ERROR] frpc_service_manager 打包失败!
    pause
    exit /b 1
)
echo [OK] frpc_service_manager.exe 打包完成
echo.

echo 清理临时文件...
rmdir /s /q build 2>nul
del frps_lite_gui.spec frpc_lite_gui.spec frps_service_manager.spec frpc_service_manager.spec 2>nul

echo.
echo ========================================
echo   打包完成! 输出在 dist\ 目录:
echo   dist\frps_lite_gui.exe          (服务端 GUI)
echo   dist\frpc_lite_gui.exe          (客户端 GUI)
echo   dist\frps_service_manager.exe   (服务端服务管理器)
echo   dist\frpc_service_manager.exe   (客户端服务管理器)
echo   ✅ 纯窗口应用，无控制台黑框
echo ========================================
pause