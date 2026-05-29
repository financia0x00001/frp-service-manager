"""
frp-lite 客户端服务管理器
=========================
提供客户端系统服务的可视化管理界面
"""

import json
import os
import subprocess
import sys
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class ClientServiceManagerGUI:
    """客户端服务管理器主窗口"""
    
    SERVICE_NAME = "FrpLiteClient"
    SERVICE_DISPLAY_NAME = "frp-lite Client"
    SERVICE_DESC = "frp-lite 内网穿透客户端"
    
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("frp-lite 客户端服务管理器")
        self.root.geometry("580x520")
        self.root.minsize(540, 480)
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
        ttk.Label(header_frame, text="⚙ frp-lite 客户端服务管理器", style="Header.TLabel").pack(anchor=tk.W)
        
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
        cfg_frame = ttk.LabelFrame(self.root, text="服务端连接配置", padding=15)
        cfg_frame.pack(fill=tk.X, padx=15, pady=(0, 12))
        
        ttk.Label(cfg_frame, text="服务器地址:").grid(row=0, column=0, sticky=tk.W, pady=7)
        self.server_addr_var = tk.StringVar(value="")
        ttk.Entry(cfg_frame, textvariable=self.server_addr_var, width=30).grid(row=0, column=1, padx=15)
        
        ttk.Label(cfg_frame, text="服务器端口:").grid(row=1, column=0, sticky=tk.W, pady=7)
        self.server_port_var = tk.StringVar(value="7000")
        ttk.Entry(cfg_frame, textvariable=self.server_port_var, width=30).grid(row=1, column=1, padx=15)
        
        ttk.Label(cfg_frame, text="Token:").grid(row=2, column=0, sticky=tk.W, pady=7)
        self.token_var = tk.StringVar(value="")
        token_entry = ttk.Entry(cfg_frame, textvariable=self.token_var, width=30)
        token_entry.grid(row=2, column=1, padx=15)
        
        ttk.Label(cfg_frame, text="连接池:").grid(row=3, column=0, sticky=tk.W, pady=7)
        self.pool_var = tk.StringVar(value="5")
        ttk.Entry(cfg_frame, textvariable=self.pool_var, width=30).grid(row=3, column=1, padx=15)
        
        # 代理配置
        proxy_frame = ttk.LabelFrame(self.root, text="代理配置", padding=15)
        proxy_frame.pack(fill=tk.X, padx=15, pady=(0, 15))
        
        ttk.Label(proxy_frame, text="代理配置文件:").grid(row=0, column=0, sticky=tk.W, pady=7)
        self.proxy_config_var = tk.StringVar(value="")
        ttk.Entry(proxy_frame, textvariable=self.proxy_config_var, width=32).grid(row=0, column=1, padx=10)
        ttk.Button(proxy_frame, text="浏览", command=self._browse_proxy_config, width=10).grid(row=0, column=2, padx=8)
        
        ttk.Button(proxy_frame, text="💾 应用配置并重启服务", command=self._apply_config, width=28).grid(row=1, column=0, columnspan=3, pady=(15, 0))
        
        # 说明面板
        info_frame = ttk.LabelFrame(self.root, text="使用说明", padding=12)
        info_frame.pack(fill=tk.BOTH, expand=True, padx=15)
        
        info_text = """• 安装服务: 将 frp-lite 客户端注册为 Windows 系统服务
• 卸载服务: 从系统服务中移除 frp-lite 客户端
• 启动/停止: 控制服务运行状态
• 服务启动后将在后台运行，开机自动启动
• 日志文件: frpc_service.log (与程序同目录)
• 配置文件: frpc_service_config.json

⚠️ 提示: 服务管理需要管理员权限
📝 代理配置文件格式参考:
{
  "proxies": [...],
  "visitors": [...]
}"""
        
        ttk.Label(info_frame, text=info_text, font=("Microsoft YaHei", 9), justify=tk.LEFT).pack(anchor=tk.W)
    
    def _run_command(self, cmd):
        """运行命令并返回结果"""
        try:
            result = subprocess.run(
                ["powershell", "-Command", cmd],
                capture_output=True,
                text=True,
                encoding='gbk',
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
            # 检查是否有活跃连接（作为服务实际运行的判断依据）
            is_listening = self._check_client_running()
            
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
    
    def _check_client_running(self):
        """检查客户端是否正在运行（通过检查日志文件和进程）"""
        # 方法1：检查日志文件是否有最近活动
        exe_dir = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(__file__)
        log_path = os.path.join(exe_dir, "frpc_service.log")
        if os.path.exists(log_path):
            import time
            mtime = os.path.getmtime(log_path)
            # 如果日志文件在最近30秒内被修改，认为服务正在运行
            if time.time() - mtime < 30:
                return True
        
        # 方法2：检查是否有客户端进程在运行
        success, stdout, _ = self._run_command('tasklist | Select-String "frpc_service_manager"')
        if success and "frpc_service_manager.exe" in stdout:
            # 检查是否有多个进程（一个是GUI，一个是服务）
            lines = stdout.strip().split('\n')
            if len(lines) >= 2:
                return True
        
        return False
    
    def _install_service(self):
        """安装服务"""
        if not self._check_admin():
            messagebox.showerror("错误", "需要管理员权限！\n请右键以管理员身份运行此程序。")
            return
        
        # 验证必要配置
        if not self.server_addr_var.get().strip():
            messagebox.showerror("错误", "服务器地址不能为空！")
            return
        if not self.token_var.get().strip():
            messagebox.showerror("错误", "Token不能为空！")
            return
        
        # 保存配置
        self._save_config()
        
        # 获取当前可执行文件路径
        exe_path = os.path.abspath(sys.executable) if not getattr(sys, 'frozen', False) else sys.executable
        
        # 使用 sc.exe 创建服务
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
    
    def _start_service(self):
        """启动服务"""
        # 确保配置已保存
        self._save_config()
        
        success, stdout, stderr = self._run_command(f"sc.exe start {self.SERVICE_NAME}")
        
        if success or "已启动" in stdout or "START_PENDING" in stdout:
            import time
            for i in range(10):
                time.sleep(1)
                if self._check_client_running():
                    messagebox.showinfo("成功", "服务启动成功！")
                    self._refresh_status()
                    return
            messagebox.showinfo("提示", "服务正在启动中，请稍后查看状态。")
        else:
            if self._check_client_running():
                messagebox.showinfo("成功", "服务已经在运行中！")
            else:
                messagebox.showerror("失败", f"服务启动失败:\n{stderr}")
        
        self._refresh_status()
    
    def _stop_service(self):
        """停止服务"""
        # 先尝试常规的 sc.exe stop
        self._run_command(f"sc.exe stop {self.SERVICE_NAME}")
        
        import time
        
        # 检查服务是否停止
        for i in range(3):
            time.sleep(1)
            if not self._check_client_running():
                messagebox.showinfo("成功", "服务停止成功！")
                self._refresh_status()
                return
        
        # 如果服务仍在运行，尝试找到客户端进程并终止
        success, stdout, _ = self._run_command('tasklist | Select-String "frpc_service_manager"')
        if success and "frpc_service_manager" in stdout:
            self._run_command('taskkill /f /im frpc_service_manager.exe')
            
            for i in range(10):
                time.sleep(1)
                if not self._check_client_running():
                    messagebox.showinfo("成功", "服务停止成功！")
                    self._refresh_status()
                    return
        
        messagebox.showerror("失败", "服务停止超时，请手动结束进程。")
        self._refresh_status()
    
    def _browse_proxy_config(self):
        """浏览选择代理配置文件"""
        path = filedialog.askopenfilename(
            filetypes=[("JSON files", "*.json")],
            title="选择代理配置文件"
        )
        if path:
            self.proxy_config_var.set(path)
    
    def _save_config(self):
        """保存配置到文件"""
        config = {
            "server": {
                "addr": self.server_addr_var.get(),
                "port": int(self.server_port_var.get()),
                "token": self.token_var.get(),
                "pool_count": int(self.pool_var.get()),
            },
            "proxy_config_file": self.proxy_config_var.get()
        }
        config_path = os.path.join(os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(__file__), "frpc_service_config.json")
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2)
    
    def _apply_config(self):
        """应用配置并重启服务"""
        if not self._check_admin():
            messagebox.showerror("错误", "需要管理员权限！")
            return
        
        # 验证必要配置
        if not self.server_addr_var.get().strip():
            messagebox.showerror("错误", "服务器地址不能为空！")
            return
        if not self.token_var.get().strip():
            messagebox.showerror("错误", "Token不能为空！")
            return
        
        self._save_config()
        
        is_running = self._status.get() == "🟢 服务运行中" or self._check_client_running()
        
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
    # 首先设置日志
    exe_dir = os.path.dirname(sys.executable)
    log_path = os.path.join(exe_dir, "frpc_service.log")
    
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.FileHandler(log_path, encoding='utf-8')]
    )
    log = logging.getLogger("frpc")
    
    try:
        log.info("frp-lite 客户端服务启动中...")
        
        # 获取配置
        config_path = os.path.join(exe_dir, "frpc_service_config.json")
        log.info(f"配置文件路径: {config_path}")
        
        config = {
            "server": {
                "addr": "",
                "port": 7000,
                "token": "",
                "pool_count": 5,
            },
            "proxy_config_file": ""
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
        from frpc_lite import ControlSession
        log.info("模块导入成功")
        
        server_addr = config["server"]["addr"]
        server_port = int(config["server"]["port"])
        token = config["server"]["token"]
        pool_count = int(config["server"]["pool_count"])
        
        if not server_addr or not token:
            log.error("服务器地址或Token未配置！")
            return
        
        log.info(f"连接到服务端: {server_addr}:{server_port}")
        
        # 加载代理配置
        proxies = []
        visitors = []
        proxy_config_file = config.get("proxy_config_file", "")
        if proxy_config_file and os.path.exists(proxy_config_file):
            try:
                with open(proxy_config_file, 'r', encoding='utf-8') as f:
                    proxy_config = json.load(f)
                    proxies = proxy_config.get("proxies", [])
                    visitors = proxy_config.get("visitors", [])
                log.info(f"加载代理配置: {len(proxies)} 个代理, {len(visitors)} 个访问者")
            except Exception as e:
                log.error(f"加载代理配置失败: {e}")
        
        # 启动客户端
        session = ControlSession(server_addr, server_port, token, pool_count)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        async def _run_client():
            try:
                ok = await session.connect()
                if not ok:
                    log.error("连接失败，检查服务器地址和Token")
                    return
                
                log.info("连接成功！")
                await session.run(proxies, visitors)
            except Exception as e:
                log.error(f"客户端异常: {e}", exc_info=True)
            finally:
                log.info("客户端连接断开")
        
        loop.run_until_complete(_run_client())
        log.info("客户端服务已停止")
        
    except Exception as e:
        log.error(f"服务异常: {e}", exc_info=True)
        import traceback
        log.error(f"堆栈跟踪:\n{traceback.format_exc()}")


if __name__ == "__main__":
    # 检查是否是服务模式
    if len(sys.argv) > 1 and sys.argv[1] == "--service":
        _run_as_service()
    else:
        # 启动 GUI
        root = tk.Tk()
        app = ClientServiceManagerGUI(root)
        root.mainloop()