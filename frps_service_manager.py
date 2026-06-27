"""
frp-lite 服务管理器
===================
提供系统服务的可视化管理界面
"""

import json
import os
import subprocess
import sys
import tkinter as tk
from tkinter import ttk, messagebox

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class ServiceManagerGUI:
    """服务管理器主窗口"""
    
    SERVICE_NAME = "FrpLiteServer"
    SERVICE_DISPLAY_NAME = "frp-lite Server"
    SERVICE_DESC = "frp-lite 内网穿透服务端"
    
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("frp-lite 服务管理器")
        self.root.geometry("520x380")
        self.root.minsize(480, 340)
        self.root.resizable(False, False)
        
        self._status = tk.StringVar(value="检查中...")
        self._status_color = tk.StringVar(value="#e67e22")
        
        self._build_ui()
        self._refresh_status()
    
    def _build_ui(self):
        """构建界面"""
        self.root.configure(bg="#f5f5f5")
        
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background="#f5f5f5")
        style.configure("TLabel", background="#f5f5f5", font=("Microsoft YaHei", 10))
        style.configure("Header.TLabel", font=("Microsoft YaHei", 16, "bold"), foreground="#2c3e50")
        style.configure("Status.TLabel", font=("Microsoft YaHei", 12, "bold"))
        
        # 标题
        header_frame = ttk.Frame(self.root)
        header_frame.pack(fill=tk.X, padx=15, pady=(15, 10))
        ttk.Label(header_frame, text="⚙ frp-lite 服务管理器", style="Header.TLabel").pack(anchor=tk.W)
        
        # 状态面板
        status_frame = ttk.LabelFrame(self.root, text="服务状态", padding=15)
        status_frame.pack(fill=tk.X, padx=15, pady=(0, 12))
        
        self.status_label = ttk.Label(status_frame, textvariable=self._status, style="Status.TLabel")
        self.status_label.configure(foreground=self._status_color.get())
        self.status_label.pack(anchor=tk.W)
        
        # 控制按钮
        btn_frame = ttk.Frame(self.root)
        btn_frame.pack(fill=tk.X, padx=15, pady=(0, 12))
        
        self.install_btn = ttk.Button(btn_frame, text="📦 安装服务", command=self._install_service, width=14)
        self.install_btn.pack(side=tk.LEFT, padx=5)
        
        self.uninstall_btn = ttk.Button(btn_frame, text="🗑 卸载服务", command=self._uninstall_service, width=14)
        self.uninstall_btn.pack(side=tk.LEFT, padx=5)
        
        self.start_btn = ttk.Button(btn_frame, text="▶ 启动服务", command=self._start_service, width=14)
        self.start_btn.pack(side=tk.LEFT, padx=5)
        
        self.stop_btn = ttk.Button(btn_frame, text="■ 停止服务", command=self._stop_service, width=14)
        self.stop_btn.pack(side=tk.LEFT, padx=5)
        
        # 配置面板
        cfg_frame = ttk.LabelFrame(self.root, text="服务配置", padding=12)
        cfg_frame.pack(fill=tk.X, padx=15, pady=(0, 12))
        
        ttk.Label(cfg_frame, text="绑定地址:").grid(row=0, column=0, sticky=tk.W, pady=5)
        self.bind_addr_var = tk.StringVar(value="0.0.0.0")
        ttk.Entry(cfg_frame, textvariable=self.bind_addr_var, width=20).grid(row=0, column=1, padx=10)
        
        ttk.Label(cfg_frame, text="绑定端口:").grid(row=1, column=0, sticky=tk.W, pady=5)
        self.bind_port_var = tk.StringVar(value="7000")
        ttk.Entry(cfg_frame, textvariable=self.bind_port_var, width=20).grid(row=1, column=1, padx=10)
        
        ttk.Label(cfg_frame, text="Token:").grid(row=2, column=0, sticky=tk.W, pady=5)
        self.token_var = tk.StringVar(value="")
        token_entry = ttk.Entry(cfg_frame, textvariable=self.token_var, width=20)
        token_entry.grid(row=2, column=1, padx=10)
        ttk.Button(cfg_frame, text="随机生成", command=self._gen_token, width=10).grid(row=2, column=2, padx=5)

        ttk.Label(cfg_frame, text="HTTP端口:").grid(row=3, column=0, sticky=tk.W, pady=5)
        self.vhost_http_port_var = tk.StringVar(value="0")
        ttk.Entry(cfg_frame, textvariable=self.vhost_http_port_var, width=20).grid(row=3, column=1, padx=10)

        ttk.Label(cfg_frame, text="HTTPS端口:").grid(row=4, column=0, sticky=tk.W, pady=5)
        self.vhost_https_port_var = tk.StringVar(value="0")
        ttk.Entry(cfg_frame, textvariable=self.vhost_https_port_var, width=20).grid(row=4, column=1, padx=10)

        ttk.Label(cfg_frame, text="证书文件:").grid(row=5, column=0, sticky=tk.W, pady=5)
        self.cert_file_var = tk.StringVar(value="")
        ttk.Entry(cfg_frame, textvariable=self.cert_file_var, width=20).grid(row=5, column=1, padx=10)

        ttk.Label(cfg_frame, text="私钥文件:").grid(row=6, column=0, sticky=tk.W, pady=5)
        self.key_file_var = tk.StringVar(value="")
        ttk.Entry(cfg_frame, textvariable=self.key_file_var, width=20).grid(row=6, column=1, padx=10)

        ttk.Label(cfg_frame, text="子域名根:").grid(row=7, column=0, sticky=tk.W, pady=5)
        self.subdomain_host_var = tk.StringVar(value="")
        ttk.Entry(cfg_frame, textvariable=self.subdomain_host_var, width=20).grid(row=7, column=1, padx=10)

        ttk.Button(cfg_frame, text="💾 应用配置并重启服务", command=self._apply_config).grid(row=8, column=0, columnspan=3, pady=(10, 0))
        
        # 说明面板
        info_frame = ttk.LabelFrame(self.root, text="使用说明", padding=12)
        info_frame.pack(fill=tk.BOTH, expand=True, padx=15)
        
        info_text = """• 安装服务: 将 frp-lite 注册为 Windows 系统服务
• 卸载服务: 从系统服务中移除 frp-lite
• 启动/停止: 控制服务运行状态
• 服务启动后将在后台运行，开机自动启动
• 日志文件: frps_service.log (与程序同目录)
• 配置文件: frps_service_config.json

⚠️ 提示: 服务管理需要管理员权限"""
        
        ttk.Label(info_frame, text=info_text, font=("Microsoft YaHei", 9), justify=tk.LEFT).pack(anchor=tk.W)
    
    def _run_command(self, cmd):
        """运行命令并返回结果"""
        try:
            result = subprocess.run(
                ["powershell", "-Command", cmd],
                capture_output=True,
                text=True,
                encoding='gbk',  # 使用 gbk 处理中文
                errors='ignore',
                shell=True
            )
            return result.returncode == 0, result.stdout.strip(), result.stderr.strip()
        except Exception as e:
            return False, "", str(e)
    
    def _refresh_status(self):
        """刷新服务状态"""
        success, stdout, stderr = self._run_command(
            f"Get-Service -Name {self.SERVICE_NAME} -ErrorAction SilentlyContinue"
        )
        
        if not success or "不存在" in stderr or "not found" in stderr.lower():
            self._status.set("服务未安装")
            self._status_color.set("#95a5a6")
            self._update_button_states(installed=False, running=False)
        else:
            # 检查端口是否在监听（作为服务实际运行的判断依据）
            bind_port = 7000
            try:
                exe_dir = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(__file__)
                config_path = os.path.join(exe_dir, "frps_service_config.json")
                if os.path.exists(config_path):
                    with open(config_path, 'r', encoding='utf-8') as f:
                        config = json.load(f)
                        bind_port = int(config.get("bind_port", 7000))
            except:
                pass
            
            is_listening = self._check_port_listening(bind_port)
            
            if "Running" in stdout or is_listening:
                self._status.set("🟢 服务运行中")
                self._status_color.set("#27ae60")
                self._update_button_states(installed=True, running=True)
            else:
                self._status.set("⏸ 服务已停止")
                self._status_color.set("#e67e22")
                self._update_button_states(installed=True, running=False)
        
        self.status_label.configure(foreground=self._status_color.get())
    
    def _update_button_states(self, installed, running):
        """更新按钮状态"""
        self.install_btn.config(state=tk.NORMAL if not installed else tk.DISABLED)
        self.uninstall_btn.config(state=tk.NORMAL if installed else tk.DISABLED)
        self.start_btn.config(state=tk.NORMAL if installed and not running else tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL if installed and running else tk.DISABLED)
    
    def _install_service(self):
        """安装服务"""
        if not self._check_admin():
            messagebox.showerror("错误", "需要管理员权限！\n请右键以管理员身份运行此程序。")
            return
        
        # 保存配置
        self._save_config()
        
        # 获取当前可执行文件路径
        exe_path = os.path.abspath(sys.executable) if not getattr(sys, 'frozen', False) else sys.executable
        
        # 使用 sc.exe 创建服务（PowerShell 中 sc 是 Set-Content 的别名）
        cmd = f'''sc.exe create "{self.SERVICE_NAME}" binPath= "{exe_path} --service" DisplayName= "{self.SERVICE_DISPLAY_NAME}" start= auto'''
        success, stdout, stderr = self._run_command(cmd)
        
        if success:
            # 设置服务描述
            desc_cmd = f'''sc description "{self.SERVICE_NAME}" "{self.SERVICE_DESC}"'''
            self._run_command(desc_cmd)
            
            messagebox.showinfo("成功", "服务安装成功！\n服务将在系统启动时自动运行。")
        else:
            messagebox.showerror("失败", f"服务安装失败:\n{stderr}")
        
        self._refresh_status()
    
    def _uninstall_service(self):
        """卸载服务"""
        if not self._check_admin():
            messagebox.showerror("错误", "需要管理员权限！")
            return
        
        # 先停止服务
        self._run_command(f"sc.exe stop {self.SERVICE_NAME}")
        
        # 删除服务
        success, stdout, stderr = self._run_command(f"sc.exe delete {self.SERVICE_NAME}")
        
        if success:
            messagebox.showinfo("成功", "服务卸载成功！")
        else:
            messagebox.showerror("失败", f"服务卸载失败:\n{stderr}")
        
        self._refresh_status()
    
    def _check_port_listening(self, port):
        """检查端口是否正在监听"""
        success, stdout, _ = self._run_command(f'netstat -ano | Select-String ":{port}" | Select-String "LISTENING"')
        return success and "LISTENING" in stdout

    def _start_service(self):
        """启动服务"""
        exe_dir = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(__file__)
        config_path = os.path.join(exe_dir, "frps_service_config.json")
        
        bind_port = 7000
        try:
            if os.path.exists(config_path):
                with open(config_path, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    bind_port = int(config.get("bind_port", 7000))
        except:
            pass
        
        if self._check_port_listening(bind_port):
            messagebox.showinfo("成功", "服务已经在运行中！")
            self._refresh_status()
            return
        
        success, stdout, stderr = self._run_command(f"sc.exe start {self.SERVICE_NAME}")
        
        if success or "已启动" in stdout or "START_PENDING" in stdout:
            import time
            for i in range(10):
                time.sleep(1)
                if self._check_port_listening(bind_port):
                    messagebox.showinfo("成功", "服务启动成功！")
                    self._refresh_status()
                    return
            messagebox.showinfo("提示", "服务正在启动中，请稍后查看状态。")
        else:
            if self._check_port_listening(bind_port):
                messagebox.showinfo("成功", "服务已经在运行中！")
            else:
                messagebox.showerror("失败", f"服务启动失败:\n{stderr}")
        
        self._refresh_status()
    
    def _stop_service(self):
        """停止服务"""
        exe_dir = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(__file__)
        config_path = os.path.join(exe_dir, "frps_service_config.json")
        
        bind_port = 7000
        try:
            if os.path.exists(config_path):
                with open(config_path, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    bind_port = int(config.get("bind_port", 7000))
        except:
            pass
        
        # 先尝试常规的 sc.exe stop
        self._run_command(f"sc.exe stop {self.SERVICE_NAME}")
        
        import time
        import re
        
        # 检查端口是否还在监听
        for i in range(3):
            time.sleep(1)
            if not self._check_port_listening(bind_port):
                messagebox.showinfo("成功", "服务停止成功！")
                self._refresh_status()
                return
        
        # 如果端口仍然在监听，尝试通过端口找到PID并强制终止
        success, stdout, _ = self._run_command(f'netstat -ano | Select-String ":{bind_port}" | Select-String "LISTENING"')
        if success and "LISTENING" in stdout:
            # 解析PID（最后一列）
            match = re.search(r'LISTENING\s+(\d+)', stdout)
            if match:
                pid = match.group(1)
                # 强制终止进程
                self._run_command(f"taskkill /f /pid {pid}")
                
                # 等待进程终止
                for i in range(10):
                    time.sleep(1)
                    if not self._check_port_listening(bind_port):
                        messagebox.showinfo("成功", "服务停止成功！")
                        self._refresh_status()
                        return
        
        messagebox.showerror("失败", "服务停止超时，请手动结束进程。")
        self._refresh_status()
    
    def _gen_token(self):
        """生成随机 token"""
        from frp_lite_protocol import generate_token
        self.token_var.set(generate_token())
    
    def _save_config(self):
        """保存配置到文件"""
        config = {
            "bind_addr": self.bind_addr_var.get(),
            "bind_port": self.bind_port_var.get(),
            "token": self.token_var.get(),
            "vhost_http_port": self.vhost_http_port_var.get(),
            "vhost_https_port": self.vhost_https_port_var.get(),
            "cert_file": self.cert_file_var.get(),
            "key_file": self.key_file_var.get(),
            "subdomain_host": self.subdomain_host_var.get(),
        }
        config_path = os.path.join(os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(__file__), "frps_service_config.json")
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2)
    
    def _apply_config(self):
        """应用配置并重启服务"""
        if not self._check_admin():
            messagebox.showerror("错误", "需要管理员权限！")
            return
        
        self._save_config()
        
        # 检查服务是否实际运行（通过端口监听判断）
        bind_port = 7000
        try:
            exe_dir = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(__file__)
            config_path = os.path.join(exe_dir, "frps_service_config.json")
            if os.path.exists(config_path):
                with open(config_path, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    bind_port = int(config.get("bind_port", 7000))
        except:
            pass
        
        is_running = self._status.get() == "🟢 服务运行中" or self._check_port_listening(bind_port)
        
        if is_running:
            self._run_command(f"sc.exe stop {self.SERVICE_NAME}")
            import time
            time.sleep(2)
            self._run_command(f"sc.exe start {self.SERVICE_NAME}")
            messagebox.showinfo("成功", "配置已应用，服务已重启！")
        else:
            messagebox.showinfo("提示", "配置已保存，服务未运行。")
        
        self._refresh_status()
    
    def _check_admin(self):
        """检查是否以管理员身份运行"""
        try:
            import ctypes
            return ctypes.windll.shell32.IsUserAnAdmin()
        except:
            return False


def _run_as_service():
    """作为服务运行"""
    # 首先设置日志（确保任何阶段的错误都能记录）
    exe_dir = os.path.dirname(sys.executable)
    log_path = os.path.join(exe_dir, "frps_service.log")
    
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.FileHandler(log_path, encoding='utf-8')]
    )
    log = logging.getLogger("frps")
    
    try:
        log.info("frp-lite 服务启动中...")
        
        # 获取配置
        config_path = os.path.join(exe_dir, "frps_service_config.json")
        log.info(f"配置文件路径: {config_path}")
        
        config = {
            "bind_addr": "0.0.0.0",
            "bind_port": 7000,
            "token": "",
            "vhost_http_port": 0,
            "vhost_https_port": 0,
            "cert_file": "",
            "key_file": "",
            "subdomain_host": "",
        }
        
        if os.path.exists(config_path):
            log.info("配置文件存在，加载中...")
            with open(config_path, 'r', encoding='utf-8') as f:
                config.update(json.load(f))
            log.info(f"配置加载成功: {config}")
        else:
            log.warning("配置文件不存在，使用默认配置")
        
        # 导入核心模块
        log.info("导入核心模块...")
        import asyncio
        from frps_lite import FrpServer
        from frp_lite_protocol import generate_token
        log.info("模块导入成功")
        
        bind_addr = config["bind_addr"]
        bind_port = int(config["bind_port"])
        token = config["token"] or generate_token()
        vhost_http_port = int(config.get("vhost_http_port", 0))
        vhost_https_port = int(config.get("vhost_https_port", 0))
        cert_file = config.get("cert_file", "")
        key_file = config.get("key_file", "")
        subdomain_host = config.get("subdomain_host", "")

        log.info(f"frp-lite 服务启动 -> {bind_addr}:{bind_port}")
        log.info(f"Token: {token}")
        if vhost_http_port:
            log.info(f"HTTP vhost 端口: {vhost_http_port}")
        if vhost_https_port:
            log.info(f"HTTPS vhost 端口: {vhost_https_port}")
        if subdomain_host:
            log.info(f"子域名根: {subdomain_host}")

        # 启动服务器
        server = FrpServer(bind_addr, bind_port, token,
                            vhost_http_port=vhost_http_port,
                            vhost_https_port=vhost_https_port,
                            cert_file=cert_file,
                            key_file=key_file,
                            subdomain_host=subdomain_host)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        loop.run_until_complete(server.start())
        log.info("服务器启动成功，进入运行循环")
        loop.run_forever()
        
    except Exception as e:
        log.error(f"服务异常: {e}", exc_info=True)
        import traceback
        log.error(f"堆栈跟踪:\n{traceback.format_exc()}")
    except BaseException as e:
        log.error(f"服务终止: {e}", exc_info=True)
    finally:
        try:
            if 'loop' in dir() and loop:
                loop.run_until_complete(server.stop())
                loop.close()
            log.info("frp-lite 服务已停止")
        except Exception as e:
            log.error(f"停止服务时出错: {e}")


if __name__ == "__main__":
    # 检查是否是服务模式
    if len(sys.argv) > 1 and sys.argv[1] == "--service":
        _run_as_service()
    else:
        # 启动 GUI
        root = tk.Tk()
        app = ServiceManagerGUI(root)
        root.mainloop()