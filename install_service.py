"""
frp-lite Windows 服务安装脚本
=============================

使用方式（需管理员权限）:
    # 安装服务
    python install_service.py install
    
    # 卸载服务
    python install_service.py remove
    
    # 启动服务
    python install_service.py start
    
    # 停止服务
    python install_service.py stop
    
    # 查询状态
    python install_service.py status

服务配置文件: frps_service_config.json
日志文件: frps_service.log
"""

import asyncio
import logging
import os
import sys
import win32serviceutil
import win32service
import win32event
import servicemanager

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from frps_lite import FrpServer
from frp_lite_protocol import generate_token


# 默认配置
DEFAULT_CONFIG = {
    "bind_addr": "0.0.0.0",
    "bind_port": 7000,
    "token": "",
    "log_file": "frps_service.log"
}


def _load_config():
    """加载配置"""
    config_path = os.path.join(os.path.dirname(__file__), "frps_service_config.json")
    if os.path.exists(config_path):
        try:
            import json
            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
                result = DEFAULT_CONFIG.copy()
                result.update(config)
                return result
        except Exception:
            pass
    return DEFAULT_CONFIG


class FrpService(win32serviceutil.ServiceFramework):
    """frp-lite 系统服务"""
    
    _svc_name_ = "FrpLiteServer"
    _svc_display_name_ = "frp-lite Server"
    _svc_description_ = "frp-lite 内网穿透服务端"

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)
        self._server = None
        self._loop = None
        self._running = False
        
    def _setup_logging(self):
        log_path = os.path.join(os.path.dirname(__file__), "frps_service.log")
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            handlers=[logging.FileHandler(log_path, encoding='utf-8')]
        )
        return logging.getLogger("frps")

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.hWaitStop)
        self._running = False
        
    def SvcDoRun(self):
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, "")
        )
        
        log = self._setup_logging()
        log.info("frp-lite 服务启动")
        
        config = _load_config()
        bind_addr = config["bind_addr"]
        bind_port = int(config["bind_port"])
        token = config["token"] or generate_token()
        
        self._server = FrpServer(bind_addr, bind_port, token)
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        
        try:
            self._loop.run_until_complete(self._server.start())
            self._running = True
            log.info(f"frp-lite 服务运行中 -> {bind_addr}:{bind_port}")
            
            while self._running:
                self._loop.run_until_complete(asyncio.sleep(1))
                
        except Exception as e:
            log.error(f"服务运行异常: {e}")
            self._running = False
        finally:
            if self._server:
                self._loop.run_until_complete(self._server.stop())
            self._loop.close()
            log.info("frp-lite 服务已停止")


def main():
    if len(sys.argv) == 1:
        # 作为服务运行
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(FrpService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        # 命令行模式
        if sys.argv[1] == "install":
            win32serviceutil.InstallService(
                None,
                FrpService._svc_name_,
                FrpService._svc_display_name_,
                startType=win32service.SERVICE_AUTO_START,
                description=FrpService._svc_description_
            )
            print(f"✅ 服务 '{FrpService._svc_display_name_}' 安装成功")
            print("   服务将在系统启动时自动运行")
            
        elif sys.argv[1] == "remove":
            win32serviceutil.StopService(FrpService._svc_name_)
            win32serviceutil.RemoveService(FrpService._svc_name_)
            print(f"✅ 服务 '{FrpService._svc_display_name_}' 卸载成功")
            
        elif sys.argv[1] == "start":
            win32serviceutil.StartService(FrpService._svc_name_)
            print(f"✅ 服务 '{FrpService._svc_display_name_}' 启动成功")
            
        elif sys.argv[1] == "stop":
            win32serviceutil.StopService(FrpService._svc_name_)
            print(f"✅ 服务 '{FrpService._svc_display_name_}' 停止成功")
            
        elif sys.argv[1] == "status":
            try:
                status = win32serviceutil.QueryServiceStatus(FrpService._svc_name_)
                status_map = {
                    win32service.SERVICE_STOPPED: "已停止",
                    win32service.SERVICE_START_PENDING: "启动中",
                    win32service.SERVICE_STOP_PENDING: "停止中",
                    win32service.SERVICE_RUNNING: "运行中",
                    win32service.SERVICE_CONTINUE_PENDING: "继续中",
                    win32service.SERVICE_PAUSE_PENDING: "暂停中",
                    win32service.SERVICE_PAUSED: "已暂停"
                }
                print(f"服务状态: {status_map.get(status[1], '未知')}")
            except Exception as e:
                print(f"❌ 查询失败: {e}")
                
        elif sys.argv[1] == "config":
            # 生成配置文件
            config = {
                "bind_addr": DEFAULT_CONFIG["bind_addr"],
                "bind_port": DEFAULT_CONFIG["bind_port"],
                "token": generate_token()
            }
            import json
            with open("frps_service_config.json", 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=2)
            print(f"✅ 配置文件已生成: frps_service_config.json")
            print(f"   默认端口: {DEFAULT_CONFIG['bind_port']}")
            print(f"   随机Token: {config['token']}")
            
        else:
            print("用法:")
            print("  install_service.py install    # 安装服务")
            print("  install_service.py remove     # 卸载服务")
            print("  install_service.py start      # 启动服务")
            print("  install_service.py stop       # 停止服务")
            print("  install_service.py status     # 查询状态")
            print("  install_service.py config     # 生成配置文件")


if __name__ == "__main__":
    main()