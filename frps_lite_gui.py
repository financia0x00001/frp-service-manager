"""
frp-lite 服务端 GUI
===================
基于 tkinter 的图形化管理界面，提供:
    - 参数配置（绑定地址、端口、Token）
    - 一键启动 / 停止服务
    - 实时状态面板（运行状态、连接数、代理数）
    - 在线客户端列表
    - 活跃代理列表
    - 实时日志输出

运行: python frps_lite_gui.py
"""

import asyncio
import json
import logging
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from frps_lite import FrpServer, ControlHandler
from frp_lite_protocol import generate_token


class AsyncEngine:
    """管理后台 asyncio 事件循环的引擎"""

    def __init__(self):
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self.server: Optional[FrpServer] = None

    @property
    def loop(self):
        return self._loop

    def start_loop(self):
        """在后台线程启动 asyncio 事件循环"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run_in_thread(self):
        self._thread = threading.Thread(target=self.start_loop, daemon=True)
        self._thread.start()
        return self._thread

    def submit(self, coro):
        """从主线程提交协程到后台循环"""
        if self._loop and self._loop.is_running():
            return asyncio.run_coroutine_threadsafe(coro, self._loop)
        return None

    def run_coro_sync(self, coro, timeout=5.0):
        """同步等待协程完成"""
        if self._loop and self._loop.is_running():
            future = asyncio.run_coroutine_threadsafe(coro, self._loop)
            try:
                return future.result(timeout=timeout)
            except Exception:
                return None
        return None

    def stop(self):
        """停止事件循环"""
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)


class LogHandler(logging.Handler):
    """将日志记录转发到 tkinter 队列"""

    def __init__(self, log_queue: queue.Queue):
        super().__init__()
        self.log_queue = log_queue
        self.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                                              datefmt="%H:%M:%S"))

    def emit(self, record):
        self.log_queue.put(self.format(record))


class ServerGUI:
    """frps 服务端主窗口"""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("frp-lite 服务端")
        self.root.geometry("900x680")
        self.root.minsize(700, 500)

        self.engine = AsyncEngine()
        self.log_queue: queue.Queue = queue.Queue()
        self._running = False
        self._refresh_id: Optional[str] = None

        self._setup_logging()
        self._build_ui()
        self._engine_running = False

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _setup_logging(self):
        root_logger = logging.getLogger("frps")
        root_logger.setLevel(logging.INFO)
        root_logger.handlers.clear()
        root_logger.addHandler(LogHandler(self.log_queue))

    def _build_ui(self):
        self.root.configure(bg="#f0f0f0")

        # ---- 样式 ----
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TLabelFrame", background="#f0f0f0")
        style.configure("TFrame", background="#f0f0f0")
        style.configure("TLabel", background="#f0f0f0", font=("Microsoft YaHei", 9))
        style.configure("TButton", font=("Microsoft YaHei", 9), padding=4)
        style.configure("Header.TLabel", font=("Microsoft YaHei", 18, "bold"),
                        foreground="#2c3e50", background="#f0f0f0")
        style.configure("Title.TLabel", font=("Microsoft YaHei", 12, "bold"),
                        foreground="#2c3e50", background="#f0f0f0")
        style.configure("Treeview", font=("Microsoft YaHei", 9), rowheight=24)
        style.configure("Treeview.Heading", font=("Microsoft YaHei", 9, "bold"))

        # ---- 顶部标题栏 ----
        header_frame = ttk.Frame(self.root)
        header_frame.pack(fill=tk.X, padx=12, pady=(10, 5))
        ttk.Label(header_frame, text="🖥  frp-lite 服务端管理", style="Header.TLabel").pack(side=tk.LEFT)

        main_frame = ttk.Frame(self.root)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        # ---- 左侧面板 ----
        left_frame = ttk.Frame(main_frame, width=380)
        left_frame.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 5))
        left_frame.pack_propagate(False)

        # 配置区
        cfg_frame = ttk.LabelFrame(left_frame, text="⚙ 服务配置", padding=10)
        cfg_frame.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(cfg_frame, text="绑定地址:").grid(row=0, column=0, sticky=tk.W, pady=3)
        self.bind_addr_var = tk.StringVar(value="0.0.0.0")
        ttk.Entry(cfg_frame, textvariable=self.bind_addr_var, width=22).grid(row=0, column=1, padx=5, pady=3)

        ttk.Label(cfg_frame, text="绑定端口:").grid(row=1, column=0, sticky=tk.W, pady=3)
        self.bind_port_var = tk.StringVar(value="7000")
        ttk.Entry(cfg_frame, textvariable=self.bind_port_var, width=22).grid(row=1, column=1, padx=5, pady=3)

        ttk.Label(cfg_frame, text="Token:").grid(row=2, column=0, sticky=tk.W, pady=3)
        self.token_var = tk.StringVar(value="")
        token_frame = ttk.Frame(cfg_frame)
        token_frame.grid(row=2, column=1, padx=5, pady=3, sticky=tk.W)
        ttk.Entry(token_frame, textvariable=self.token_var, width=15).pack(side=tk.LEFT)
        ttk.Button(token_frame, text="随机生成", command=self._gen_token, width=9).pack(side=tk.LEFT, padx=4)

        # 控制按钮
        btn_frame = ttk.Frame(cfg_frame)
        btn_frame.grid(row=3, column=0, columnspan=2, pady=(10, 0))

        self.start_btn = ttk.Button(btn_frame, text="▶ 启动服务", command=self._start_server, width=14)
        self.start_btn.pack(side=tk.LEFT, padx=3)

        self.stop_btn = ttk.Button(btn_frame, text="■ 停止服务", command=self._stop_server,
                                    width=14, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=3)

        self.config_save_btn = ttk.Button(btn_frame, text="💾 保存配置", command=self._save_config, width=12)
        self.config_save_btn.pack(side=tk.LEFT, padx=3)

        self.config_load_btn = ttk.Button(btn_frame, text="📂 加载配置", command=self._load_config, width=12)
        self.config_load_btn.pack(side=tk.LEFT, padx=3)

        # 状态区
        status_frame = ttk.LabelFrame(left_frame, text="📊 服务状态", padding=10)
        status_frame.pack(fill=tk.X, pady=(0, 8))

        self.status_var = tk.StringVar(value="⏸ 未启动")
        self.status_label = tk.Label(status_frame, textvariable=self.status_var,
                                      font=("Microsoft YaHei", 13, "bold"),
                                      fg="#e67e22", bg="#f0f0f0")
        self.status_label.pack(anchor=tk.W)

        stats_inner = ttk.Frame(status_frame)
        stats_inner.pack(fill=tk.X, pady=(8, 0))

        ttk.Label(stats_inner, text="在线客户端:").grid(row=0, column=0, sticky=tk.W)
        self.clients_count_var = tk.StringVar(value="0")
        tk.Label(stats_inner, textvariable=self.clients_count_var,
                font=("Microsoft YaHei", 11, "bold"), fg="#2980b9", bg="#f0f0f0").grid(row=0, column=1, padx=15)

        ttk.Label(stats_inner, text="活跃代理:").grid(row=0, column=2, sticky=tk.W)
        self.proxies_count_var = tk.StringVar(value="0")
        tk.Label(stats_inner, textvariable=self.proxies_count_var,
                font=("Microsoft YaHei", 11, "bold"), fg="#27ae60", bg="#f0f0f0").grid(row=0, column=3, padx=15)

        ttk.Label(stats_inner, text="运行时间:").grid(row=1, column=0, sticky=tk.W, pady=(6, 0))
        self.uptime_var = tk.StringVar(value="--")
        tk.Label(stats_inner, textvariable=self.uptime_var,
                font=("Microsoft YaHei", 10), fg="#7f8c8d", bg="#f0f0f0").grid(row=1, column=1, padx=15, pady=(6, 0))

        # 客户端列表
        clients_frame = ttk.LabelFrame(left_frame, text="👤 在线客户端", padding=8)
        clients_frame.pack(fill=tk.BOTH, expand=True)

        self.clients_tree = ttk.Treeview(clients_frame, columns=("addr", "proxies"),
                                          show="headings", height=5)
        self.clients_tree.heading("addr", text="客户端地址")
        self.clients_tree.heading("proxies", text="代理数")
        self.clients_tree.column("addr", width=200)
        self.clients_tree.column("proxies", width=80)
        self.clients_tree.pack(fill=tk.BOTH, expand=True)

        # ---- 右侧面板 ----
        right_frame = ttk.Frame(main_frame, width=460)
        right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)

        # 代理列表
        proxy_frame = ttk.LabelFrame(right_frame, text="🔗 活跃代理", padding=8)
        proxy_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

        self.proxy_tree = ttk.Treeview(proxy_frame, columns=("name", "type", "port", "client"),
                                        show="headings")
        self.proxy_tree.heading("name", text="名称")
        self.proxy_tree.heading("type", text="类型")
        self.proxy_tree.heading("port", text="端口")
        self.proxy_tree.heading("client", text="客户端")
        self.proxy_tree.column("name", width=100)
        self.proxy_tree.column("type", width=60)
        self.proxy_tree.column("port", width=60)
        self.proxy_tree.column("client", width=180)
        self.proxy_tree.pack(fill=tk.BOTH, expand=True)

        # 日志区
        log_frame = ttk.LabelFrame(right_frame, text="📜 运行日志", padding=5)
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.log_text = scrolledtext.ScrolledText(log_frame, height=10, wrap=tk.WORD,
                                                   font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4",
                                                   insertbackground="white")
        self.log_text.pack(fill=tk.BOTH, expand=True)
        self.log_text.configure(state=tk.DISABLED)

        # 颜色标签 (用于高亮不同级别日志)
        self.log_text.tag_configure("WARNING", foreground="#e5c07b")
        self.log_text.tag_configure("ERROR", foreground="#e06c75")
        self.log_text.tag_configure("INFO", foreground="#98c379")

    def _gen_token(self):
        self.token_var.set(generate_token(12))

    def _save_config(self):
        config = {
            "bind_addr": self.bind_addr_var.get(),
            "bind_port": int(self.bind_port_var.get()),
            "token": self.token_var.get(),
        }
        try:
            from tkinter import filedialog
            path = filedialog.asksaveasfilename(
                defaultextension=".json",
                filetypes=[("JSON files", "*.json")],
                initialfile="frps_config.json",
            )
            if path:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(config, f, indent=2, ensure_ascii=False)
                self._log_message(f"配置已保存到: {path}", "INFO")
        except Exception as e:
            self._log_message(f"保存配置失败: {e}", "ERROR")

    def _load_config(self):
        try:
            from tkinter import filedialog
            path = filedialog.askopenfilename(filetypes=[("JSON files", "*.json")])
            if path and os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                self.bind_addr_var.set(config.get("bind_addr", "0.0.0.0"))
                self.bind_port_var.set(str(config.get("bind_port", 7000)))
                self.token_var.set(config.get("token", ""))
                self._log_message(f"配置已加载: {path}", "INFO")
        except Exception as e:
            self._log_message(f"加载配置失败: {e}", "ERROR")

    def _start_server(self):
        if not self._engine_running:
            self.engine.run_in_thread()
            self._engine_running = True

        bind_addr = self.bind_addr_var.get().strip()
        try:
            bind_port = int(self.bind_port_var.get())
        except ValueError:
            messagebox.showerror("错误", "端口号必须为数字")
            return
        token = self.token_var.get().strip()

        if not token:
            messagebox.showerror("错误", "Token 不能为空")
            return

        def _do_start():
            self.engine.server = FrpServer(bind_addr, bind_port, token)
            try:
                future = self.engine.submit(self.engine.server.start())
                future.add_done_callback(self._on_server_started)
            except Exception as e:
                self._log_message(f"启动失败: {e}", "ERROR")

        # 需要在主线程中更新 UI
        self.start_btn.configure(state=tk.DISABLED)
        self._log_message(f"正在启动服务 {bind_addr}:{bind_port} ...", "INFO")

        # 延迟提交到 asyncio 线程
        self.root.after(200, _do_start)

    def _on_server_started(self, future):
        try:
            self._running = True
            self._start_time = time.time()
            self.root.after(0, self._update_ui_running)
            self._log_message(f"服务启动成功! Token={self.engine.server.token}", "INFO")
            self._start_refresh()
        except Exception as e:
            self.root.after(0, lambda: self._log_message(f"服务异常: {e}", "ERROR"))

    def _update_ui_running(self):
        self.status_var.set("🟢 运行中")
        self.status_label.configure(fg="#27ae60")
        self.start_btn.configure(state=tk.DISABLED)
        self.stop_btn.configure(state=tk.NORMAL)

    def _stop_server(self):
        if self.engine.server:
            self.engine.submit(self.engine.server.stop())

        self._running = False
        self._stop_refresh()
        self.status_var.set("⏸ 已停止")
        self.status_label.configure(fg="#e67e22")
        self.start_btn.configure(state=tk.NORMAL)
        self.stop_btn.configure(state=tk.DISABLED)
        self.uptime_var.set("--")
        self.clients_count_var.set("0")
        self.proxies_count_var.set("0")
        self._log_message("服务已停止", "INFO")

    def _start_refresh(self):
        self._poll_logs()
        self._refresh_data()

    def _stop_refresh(self):
        if self._refresh_id:
            self.root.after_cancel(self._refresh_id)
            self._refresh_id = None

    def _poll_logs(self):
        """轮询日志队列并显示"""
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self._append_log(msg)
        except queue.Empty:
            pass
        if self._running:
            self.root.after(300, self._poll_logs)

    def _append_log(self, msg: str):
        self.log_text.configure(state=tk.NORMAL)
        if "[WARNING]" in msg or "[WARN]" in msg:
            tag = "WARNING"
        elif "[ERROR]" in msg:
            tag = "ERROR"
        else:
            tag = "INFO"
        self.log_text.insert(tk.END, msg + "\n", tag)
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _log_message(self, msg: str, level: str = "INFO"):
        """手动添加一条日志"""
        formatted = f"{time.strftime('%H:%M:%S')} [{level}] {msg}"
        self._append_log(formatted)

    def _refresh_data(self):
        """定期刷新状态数据"""
        if not self._running:
            return

        server = self.engine.server
        if server:
            # 更新客户端列表
            current_clients = set()
            for item in self.clients_tree.get_children():
                current_clients.add(self.clients_tree.item(item, "values")[0] if self.clients_tree.item(item, "values") else "")

            live_clients = {}
            for cid, ctl in list(server._controls.items()):
                addr = getattr(ctl, "peer_addr", "") or cid
                if addr:
                    proxy_count = len(getattr(ctl, "_proxies", {}))
                    live_clients[addr] = proxy_count

            # 更新 tree
            existing = {self.clients_tree.item(i, "values")[0]: i
                       for i in self.clients_tree.get_children()
                       if self.clients_tree.item(i, "values")}

            for addr, count in live_clients.items():
                if addr in existing:
                    self.clients_tree.item(existing[addr], values=(addr, count))
                else:
                    self.clients_tree.insert("", tk.END, values=(addr, count))

            for addr, item in existing.items():
                if addr not in live_clients:
                    self.clients_tree.delete(item)

            self.clients_count_var.set(str(len(live_clients)))

            # 更新代理列表
            proxy_existing = {self.proxy_tree.item(i, "values")[0]: i
                            for i in self.proxy_tree.get_children()
                            if self.proxy_tree.item(i, "values")}

            for name, info in server._proxies.items():
                ptype = info.get("type", "?")
                port = str(info.get("remote_port", "-"))
                run_id = info.get("run_id", "?")[:12]
                vals = (name, ptype, port, run_id)
                if name in proxy_existing:
                    self.proxy_tree.item(proxy_existing[name], values=vals)
                else:
                    self.proxy_tree.insert("", tk.END, values=vals)

            for name, item in proxy_existing.items():
                if name not in server._proxies:
                    self.proxy_tree.delete(item)

            self.proxies_count_var.set(str(len(server._proxies)))

            # 更新运行时间
            if hasattr(self, '_start_time'):
                elapsed = int(time.time() - self._start_time)
                h, m, s = elapsed // 3600, (elapsed % 3600) // 60, elapsed % 60
                self.uptime_var.set(f"{h:02d}:{m:02d}:{s:02d}")

        self._refresh_id = self.root.after(2000, self._refresh_data)

    def _on_close(self):
        if self._running:
            self._stop_server()
        self.engine.stop()
        self.root.destroy()


def main():
    root = tk.Tk()
    app = ServerGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()