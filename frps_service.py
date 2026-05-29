"""
frp-lite Windows 系统服务模块
=============================
提供系统服务安装、卸载、启动、停止功能

使用方式:
    # 安装服务
    python frps_service.py install
    
    # 卸载服务
    python frps_service.py remove
    
    # 启动服务
    python frps_service.py start
    
    # 停止服务
    python frps_service.py stop
    
    # 查询状态
    python frps_service.py status
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


# 默认服务配置
DEFAULT_CONFIG = {
    "bind_addr": "0.0.0.0",
    "bind_port": 7000,
    "token": "",  # 为空则自动生成随机token
    "log_file": "frps_service.log"
}


def _load_config():
    """从配置文件加载设置，如果不存在则返回默认配置"""
    config_path = os.path.join(os.path.dirname(__file__), "frps_service_config.json")
    if os.path.exists(config_path):
        try:
            import json
            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
                # 合并配置（使用文件配置覆盖默认配置）
                result = DEFAULT_CONFIG.copy()
                result.update(config)
                return result
        except Exception:
            pass
    return DEFAULT_CONFIG


class FrpService(win32serviceutil.ServiceFramework):
    """frp-lite Windows 系统服务"""
    
    _svc_name_ = "FrpLiteServer"
    _svc_display_name_ = "frp-lite Server"
    _svc_description_ = "frp-lite 内网穿透服务端 - 提供 TCP/UDP/XTCP 代理服务"

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)
        self._server = None
        self._loop = None
        self._running = False
        
    def _setup_logging(self):
        """配置日志输出到文件"""
        log_path = os.path.join(os.path.dirname(__file__), DEFAULT_CONFIG["log_file"])
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            handlers=[
                logging.FileHandler(log_path, encoding='utf-8'),
            ]
        )
        return logging.getLogger("frps")

    def SvcStop(self):
        """停止服务"""
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.hWaitStop)
        self._running = False
        
    def SvcDoRun(self):
        """服务主循环"""
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, "")
        )
        
        log = self._setup_logging()
        log.info("frp-lite 服务启动")
        
        # 加载配置（优先从文件读取）
        config = _load_config()
        bind_addr = config["bind_addr"]
        bind_port = int(config["bind_port"])
        token = config["token"] or generate_token()
        
        # 创建服务器
        self._server = FrpServer(bind_addr, bind_port, token)
        
        # 启动 asyncio 事件循环
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        
        try:
            # 启动服务
            self._loop.run_until_complete(self._server.start())
            self._running = True
            log.info(f"frp-lite 服务运行中 -> {bind_addr}:{bind_port}")
            
            # 等待停止信号
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


def run_as_console():
    """控制台模式运行（用于测试）"""
    import argparse
    
    parser = argparse.ArgumentParser(description="frp-lite Windows 服务管理")
    parser.add_argument("--install", action="store_true", help="安装服务")
    parser.add_argument("--remove", action="store_true", help="卸载服务")
    parser.add_argument("--start", action="store_true", help="启动服务")
    parser.add_argument("--stop", action="store_true", help="停止服务")
    parser.add_argument("--status", action="store_true", help="查询状态")
    parser.add_argument("--run", action="store_true", help="控制台模式运行")
    
    args = parser.parse_args()
    
    if args.install:
        win32serviceutil.InstallService(
            None,
            FrpService._svc_name_,
            FrpService._svc_display_name_,
            startType=win32service.SERVICE_AUTO_START,
            description=FrpService._svc_description_
        )
        print(f"服务 '{FrpService._svc_display_name_}' 安装成功")
        print("服务将在系统启动时自动运行")
        
    elif args.remove:
        win32serviceutil.RemoveService(FrpService._svc_name_)
        print(f"服务 '{FrpService._svc_display_name_}' 卸载成功")
        
    elif args.start:
        win32serviceutil.StartService(FrpService._svc_name_)
        print(f"服务 '{FrpService._svc_display_name_}' 启动成功")
        
    elif args.stop:
        win32serviceutil.StopService(FrpService._svc_name_)
        print(f"服务 '{FrpService._svc_display_name_}' 停止成功")
        
    elif args.status:
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
        
    elif args.run:
        # 控制台模式运行
        log = logging.getLogger("frps")
        log.setLevel(logging.INFO)
        log.addHandler(logging.StreamHandler())
        
        bind_addr = DEFAULT_CONFIG["bind_addr"]
        bind_port = DEFAULT_CONFIG["bind_port"]
        token = DEFAULT_CONFIG["token"] or generate_token()
        
        server = FrpServer(bind_addr, bind_port, token)
        loop = asyncio.get_event_loop()
        
        try:
            loop.run_until_complete(server.start())
            print(f"frp-lite 运行中 -> {bind_addr}:{bind_port}")
            print(f"Token: {token}")
            loop.run_forever()
        except KeyboardInterrupt:
            print("\n正在停止服务...")
            loop.run_until_complete(server.stop())
            loop.close()
            
    else:
        parser.print_help()


if __name__ == '__main__':
    if len(sys.argv) == 1:
        # 作为服务运行
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(FrpService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        # 命令行模式
        run_as_console()