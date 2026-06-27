"""
frp-lite 客户端 GUI
===================
基于 tkinter 的图形化管理界面，提供:
    - 服务端连接配置（地址、端口、Token）
    - 一键连接 / 断开
    - 代理配置编辑器（添加、编辑、删除 TCP/UDP/XTCP 代理）
    - 访问者配置编辑器（XTCP 访问者）
    - 配置文件导入导出
    - 实时连接状态和日志

运行: python frpc_lite_gui.py
"""

import asyncio
import json
import logging
import os
import queue
import sys
import threading
import time
import ctypes
import struct
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, filedialog
from typing import List, Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from frpc_lite import ControlSession
from frp_lite_protocol import generate_token


class AsyncClientEngine:
    """管理后台 asyncio 事件循环的引擎（客户端专用）"""

    def __init__(self):
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self.session: Optional[ControlSession] = None
        self._running = False

    @property
    def loop(self):
        return self._loop

    def start_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run_in_thread(self):
        self._thread = threading.Thread(target=self.start_loop, daemon=True)
        self._thread.start()
        return self._thread

    def submit(self, coro):
        if self._loop and self._loop.is_running():
            return asyncio.run_coroutine_threadsafe(coro, self._loop)
        return None

    def stop(self):
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)


class LogHandler(logging.Handler):
    """将日志记录转发到 GUI 队列"""

    def __init__(self, log_queue: queue.Queue):
        super().__init__()
        self.log_queue = log_queue
        self.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                                              datefmt="%H:%M:%S"))

    def emit(self, record):
        self.log_queue.put(self.format(record))


class ProxyEditDialog(tk.Toplevel):
    """代理 / 访问者 编辑对话框"""

    def __init__(self, parent, title: str, fields: List[dict],
                 initial: Optional[dict] = None):
        super().__init__(parent)
        self.title(title)
        self.result: Optional[dict] = None
        self.vars: Dict[str, tk.StringVar] = {}
        self.widgets: Dict[str, tk.Widget] = {}

        self.geometry("380x460")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        main = ttk.Frame(self, padding=12)
        main.pack(fill=tk.BOTH, expand=True)

        for i, f in enumerate(fields):
            ttk.Label(main, text=f["label"] + ":").grid(
                row=i, column=0, sticky=tk.W, pady=3)
            var = tk.StringVar(value=(
                str(initial.get(f["key"], f.get("default", "")))
                if initial else f.get("default", "")
            ))
            self.vars[f["key"]] = var

            if f.get("type") == "combo":
                w = ttk.Combobox(main, textvariable=var, values=f.get("values", []),
                                 state="readonly", width=22)
            else:
                w = ttk.Entry(main, textvariable=var, width=24)
            w.grid(row=i, column=1, padx=5, pady=3, sticky=tk.W)
            self.widgets[f["key"]] = w

        btn_frame = ttk.Frame(main)
        btn_frame.grid(row=len(fields), column=0, columnspan=2, pady=(14, 0))
        ttk.Button(btn_frame, text="确定", command=self._on_ok, width=12).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="取消", command=self.destroy, width=12).pack(side=tk.LEFT, padx=4)

        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.wait_window()

    def _on_ok(self):
        self.result = {k: v.get() for k, v in self.vars.items()}
        self.destroy()


# ==================== Windows 系统托盘支持（纯 ctypes 实现） ====================

_WM_TRAYICON = 0x0400 + 21
_WM_TASK = 0x0400 + 22
_TASK_SHOW = 1
_TASK_HIDE = 2
_TASK_DESTROY = 3
_NIM_ADD = 0x00000000
_NIM_DELETE = 0x00000002
_NIF_MESSAGE = 0x00000001
_NIF_ICON = 0x00000002
_NIF_TIP = 0x00000004
_WM_LBUTTONUP = 0x0202
_WM_RBUTTONUP = 0x0205
_WM_DESTROY = 0x0002
_IDI_APPLICATION = 32512


class _NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("hWnd", ctypes.c_void_p),
        ("uID", ctypes.c_uint),
        ("uFlags", ctypes.c_uint),
        ("uCallbackMessage", ctypes.c_uint),
        ("hIcon", ctypes.c_void_p),
        ("szTip", ctypes.c_wchar * 128),
    ]


class _WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", ctypes.c_uint),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", ctypes.c_void_p),
        ("hIcon", ctypes.c_void_p),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", ctypes.c_wchar_p),
        ("lpszClassName", ctypes.c_wchar_p),
    ]


class _MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("message", ctypes.c_uint),
        ("wParam", ctypes.c_size_t),
        ("lParam", ctypes.c_ssize_t),
        ("time", ctypes.c_uint),
        ("pt", ctypes.c_long * 2),
    ]


_WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint,
    ctypes.c_size_t, ctypes.c_ssize_t
)


def _setup_user32_argtypes():
    """为 user32 / shell32 函数设置正确的参数类型（64-bit 兼容）"""
    user32 = ctypes.windll.user32
    shell32 = ctypes.windll.shell32
    HWND = ctypes.c_void_p
    UINT = ctypes.c_uint
    WPARAM = ctypes.c_size_t
    LPARAM = ctypes.c_ssize_t
    LRESULT = ctypes.c_ssize_t
    BOOL = ctypes.c_int

    user32.DefWindowProcW.argtypes = [HWND, UINT, WPARAM, LPARAM]
    user32.DefWindowProcW.restype = LRESULT

    user32.CreateWindowExW.argtypes = [
        ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        HWND, HWND, HWND, ctypes.c_void_p
    ]
    user32.CreateWindowExW.restype = HWND

    user32.RegisterClassW.argtypes = [ctypes.c_void_p]
    user32.RegisterClassW.restype = ctypes.c_ushort

    user32.GetMessageW.argtypes = [ctypes.c_void_p, HWND, UINT, UINT]
    user32.GetMessageW.restype = BOOL

    user32.TranslateMessage.argtypes = [ctypes.c_void_p]
    user32.TranslateMessage.restype = BOOL

    user32.DispatchMessageW.argtypes = [ctypes.c_void_p]
    user32.DispatchMessageW.restype = LRESULT

    user32.PostMessageW.argtypes = [HWND, UINT, WPARAM, LPARAM]
    user32.PostMessageW.restype = BOOL

    user32.PostQuitMessage.argtypes = [ctypes.c_int]
    user32.PostQuitMessage.restype = None

    user32.DestroyWindow.argtypes = [HWND]
    user32.DestroyWindow.restype = BOOL

    user32.LoadIconW.argtypes = [HWND, ctypes.c_void_p]
    user32.LoadIconW.restype = HWND

    user32.GetCursorPos.argtypes = [ctypes.c_void_p]
    user32.GetCursorPos.restype = BOOL

    shell32.Shell_NotifyIconW.argtypes = [UINT, ctypes.c_void_p]
    shell32.Shell_NotifyIconW.restype = BOOL


_setup_user32_argtypes()


class SystemTray(threading.Thread):
    """Windows 系统托盘图标（独立线程消息循环，纯 ctypes 实现，无外部依赖）"""

    def __init__(self, root, on_show, on_exit):
        super().__init__(daemon=True)
        self.root = root
        self.on_show = on_show
        self.on_exit = on_exit
        self._hwnd = None
        self._hicon = None
        self._visible = False
        self._ready = threading.Event()
        self._wndproc_cb = None  # 防止回调被垃圾回收

    def run(self):
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        # 注册窗口类
        self._wndproc_cb = _WNDPROC(self._tray_wndproc)
        hinstance = kernel32.GetModuleHandleW(None)
        cls = _WNDCLASS()
        cls.lpfnWndProc = ctypes.cast(self._wndproc_cb, ctypes.c_void_p)
        cls.hInstance = hinstance
        cls.lpszClassName = "FrpcTrayWndClass"
        user32.RegisterClassW(ctypes.byref(cls))

        # 创建隐藏窗口
        self._hwnd = user32.CreateWindowExW(
            0, "FrpcTrayWndClass", "", 0, 0, 0, 0, 0, None, None, hinstance, None
        )

        # 加载图标
        self._hicon = user32.LoadIconW(0, _IDI_APPLICATION)

        self._ready.set()

        # 消息循环
        msg = _MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _tray_wndproc(self, hwnd, msg, wparam, lparam):
        user32 = ctypes.windll.user32

        if msg == _WM_TRAYICON:
            if lparam == _WM_LBUTTONUP:
                self.root.after(0, self.on_show)
            elif lparam == _WM_RBUTTONUP:
                self.root.after(0, self._show_context_menu)
            return 0
        elif msg == _WM_TASK:
            if wparam == _TASK_SHOW:
                self._add_icon()
            elif wparam == _TASK_HIDE:
                self._remove_icon()
            elif wparam == _TASK_DESTROY:
                self._remove_icon()
                user32.DestroyWindow(hwnd)
            return 0
        elif msg == _WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0

        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _add_icon(self):
        nid = _NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = _NIF_MESSAGE | _NIF_ICON | _NIF_TIP
        nid.uCallbackMessage = _WM_TRAYICON
        nid.hIcon = self._hicon
        nid.szTip = "frp-lite 客户端"
        ctypes.windll.shell32.Shell_NotifyIconW(_NIM_ADD, ctypes.byref(nid))
        self._visible = True

    def _remove_icon(self):
        nid = _NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        ctypes.windll.shell32.Shell_NotifyIconW(_NIM_DELETE, ctypes.byref(nid))
        self._visible = False

    def _show_context_menu(self):
        """右键托盘图标弹出 tkinter 菜单"""
        pt = ctypes.wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="显示主窗口", command=self.on_show)
        menu.add_separator()
        menu.add_command(label="退出", command=self.on_exit)
        menu.tk_popup(pt.x, pt.y)

    def show(self):
        """显示托盘图标（线程安全）"""
        self._ready.wait(timeout=5)
        if self._hwnd:
            ctypes.windll.user32.PostMessageW(self._hwnd, _WM_TASK, _TASK_SHOW, 0)

    def hide(self):
        """隐藏托盘图标（线程安全）"""
        self._ready.wait(timeout=5)
        if self._hwnd:
            ctypes.windll.user32.PostMessageW(self._hwnd, _WM_TASK, _TASK_HIDE, 0)

    def destroy(self):
        """销毁托盘图标和窗口（线程安全）"""
        self._ready.wait(timeout=5)
        if self._hwnd:
            ctypes.windll.user32.PostMessageW(self._hwnd, _WM_TASK, _TASK_DESTROY, 0)


class ClientGUI:
    """frpc 客户端主窗口"""

    PROXY_FIELDS = [
        {"key": "name", "label": "代理名称", "default": ""},
        {"key": "type", "label": "代理类型", "default": "tcp",
         "type": "combo", "values": ["tcp", "udp", "xtcp", "http", "https"]},
        {"key": "local_ip", "label": "本地地址", "default": "127.0.0.1"},
        {"key": "local_port", "label": "本地端口", "default": ""},
        {"key": "remote_port", "label": "远程端口", "default": ""},
        {"key": "custom_domains", "label": "自定义域名(HTTP/HTTPS)", "default": ""},
        {"key": "subdomain", "label": "子域名前缀(HTTP/HTTPS)", "default": ""},
        {"key": "http_user", "label": "HTTP用户名(可选)", "default": ""},
        {"key": "http_pwd", "label": "HTTP密码(可选)", "default": ""},
        {"key": "secret_key", "label": "密钥(XTCP)", "default": ""},
    ]

    VISITOR_FIELDS = [
        {"key": "name", "label": "访问者名称", "default": ""},
        {"key": "type", "label": "类型", "default": "xtcp",
         "type": "combo", "values": ["xtcp"]},
        {"key": "server_name", "label": "目标代理名", "default": ""},
        {"key": "secret_key", "label": "密钥", "default": ""},
        {"key": "bind_port", "label": "绑定端口", "default": ""},
        {"key": "local_ip", "label": "绑定地址", "default": "0.0.0.0"},
    ]

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("frp-lite 客户端")
        self.root.geometry("980x700")
        self.root.minsize(780, 550)

        self.engine = AsyncClientEngine()
        self.log_queue: queue.Queue = queue.Queue()
        self._connected = False
        self._connect_start_time: float = 0
        self._refresh_id: Optional[str] = None
        self._engine_running = False

        # 内存中的代理配置
        self._proxies: List[dict] = []
        self._visitors: List[dict] = []

        self._setup_logging()
        self._build_ui()
        self.root.update_idletasks()
        self._tray = SystemTray(root, self._restore_from_tray, self._exit_app)
        self._tray.start()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _setup_logging(self):
        root_logger = logging.getLogger("frpc")
        root_logger.setLevel(logging.INFO)
        root_logger.handlers.clear()
        root_logger.addHandler(LogHandler(self.log_queue))

    def _build_ui(self):
        self.root.configure(bg="#f0f0f0")

        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TLabelFrame", background="#f0f0f0")
        style.configure("TFrame", background="#f0f0f0")
        style.configure("TLabel", background="#f0f0f0", font=("Microsoft YaHei", 9))
        style.configure("TButton", font=("Microsoft YaHei", 9), padding=4)
        style.configure("Header.TLabel", font=("Microsoft YaHei", 18, "bold"),
                        foreground="#2c3e50", background="#f0f0f0")
        style.configure("Treeview", font=("Microsoft YaHei", 9), rowheight=24)
        style.configure("Treeview.Heading", font=("Microsoft YaHei", 9, "bold"))

        # ---- 顶部标题 ----
        header_frame = ttk.Frame(self.root)
        header_frame.pack(fill=tk.X, padx=12, pady=(10, 5))
        ttk.Label(header_frame, text="💻  frp-lite 客户端管理", style="Header.TLabel").pack(side=tk.LEFT)

        # 主体
        main_frame = ttk.Frame(self.root)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        # ---- 左侧面板 ----
        left_frame = ttk.Frame(main_frame, width=360)
        left_frame.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 5))
        left_frame.pack_propagate(False)

        # 服务器连接配置
        srv_frame = ttk.LabelFrame(left_frame, text="🌐 服务端连接", padding=10)
        srv_frame.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(srv_frame, text="服务器地址:").grid(row=0, column=0, sticky=tk.W, pady=3)
        self.server_addr_var = tk.StringVar(value="127.0.0.1")
        ttk.Entry(srv_frame, textvariable=self.server_addr_var, width=26).grid(row=0, column=1, padx=5, pady=3)

        ttk.Label(srv_frame, text="端口:").grid(row=1, column=0, sticky=tk.W, pady=3)
        self.server_port_var = tk.StringVar(value="7000")
        ttk.Entry(srv_frame, textvariable=self.server_port_var, width=26).grid(row=1, column=1, padx=5, pady=3)

        ttk.Label(srv_frame, text="Token:").grid(row=2, column=0, sticky=tk.W, pady=3)
        token_frame = ttk.Frame(srv_frame)
        token_frame.grid(row=2, column=1, padx=5, pady=3, sticky=tk.W)
        self.token_var = tk.StringVar(value="")
        ttk.Entry(token_frame, textvariable=self.token_var, width=19).pack(side=tk.LEFT)

        ttk.Label(srv_frame, text="连接池:").grid(row=3, column=0, sticky=tk.W, pady=3)
        self.pool_var = tk.StringVar(value="5")
        ttk.Entry(srv_frame, textvariable=self.pool_var, width=26).grid(row=3, column=1, padx=5, pady=3)

        # 连接按钮
        btn_frame = ttk.Frame(srv_frame)
        btn_frame.grid(row=4, column=0, columnspan=2, pady=(10, 0))

        self.connect_btn = ttk.Button(btn_frame, text="▶ 连接", command=self._connect, width=12)
        self.connect_btn.pack(side=tk.LEFT, padx=3)

        self.disconnect_btn = ttk.Button(btn_frame, text="■ 断开", command=self._disconnect,
                                          width=12, state=tk.DISABLED)
        self.disconnect_btn.pack(side=tk.LEFT, padx=3)

        self.load_cfg_btn = ttk.Button(btn_frame, text="📂 导入配置", command=self._load_config, width=12)
        self.load_cfg_btn.pack(side=tk.LEFT, padx=3)

        self.save_cfg_btn = ttk.Button(btn_frame, text="💾 导出配置", command=self._save_config, width=12)
        self.save_cfg_btn.pack(side=tk.LEFT, padx=3)

        # 状态
        status_frame = ttk.LabelFrame(left_frame, text="📊 连接状态", padding=10)
        status_frame.pack(fill=tk.X, pady=(0, 8))

        self.status_var = tk.StringVar(value="⏸ 未连接")
        self.status_label = tk.Label(status_frame, textvariable=self.status_var,
                                      font=("Microsoft YaHei", 13, "bold"),
                                      fg="#e67e22", bg="#f0f0f0")
        self.status_label.pack(anchor=tk.W)

        stats_inner = ttk.Frame(status_frame)
        stats_inner.pack(fill=tk.X, pady=(8, 0))

        ttk.Label(stats_inner, text="Run ID:").grid(row=0, column=0, sticky=tk.W)
        self.run_id_var = tk.StringVar(value="--")
        tk.Label(stats_inner, textvariable=self.run_id_var,
                font=("Consolas", 9), fg="#2980b9", bg="#f0f0f0").grid(row=0, column=1, padx=10)

        ttk.Label(stats_inner, text="代理数:").grid(row=0, column=2, sticky=tk.W)
        self.proxies_count_var = tk.StringVar(value="0")
        tk.Label(stats_inner, textvariable=self.proxies_count_var,
                font=("Microsoft YaHei", 10, "bold"), fg="#27ae60", bg="#f0f0f0").grid(row=0, column=3, padx=10)

        ttk.Label(stats_inner, text="连接时长:").grid(row=1, column=0, sticky=tk.W, pady=(6, 0))
        self.uptime_var = tk.StringVar(value="--")
        tk.Label(stats_inner, textvariable=self.uptime_var,
                font=("Microsoft YaHei", 10), fg="#7f8c8d", bg="#f0f0f0").grid(row=1, column=1, padx=10, pady=(6, 0))

        # ---- 右侧面板 ----
        right_frame = ttk.Frame(main_frame)
        right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)

        # 笔记本标签页
        notebook = ttk.Notebook(right_frame)
        notebook.pack(fill=tk.BOTH, expand=True)

        # ---- 代理标签页 ----
        proxy_tab = ttk.Frame(notebook)
        notebook.add(proxy_tab, text="🔗 代理列表")

        proxy_toolbar = ttk.Frame(proxy_tab)
        proxy_toolbar.pack(fill=tk.X, pady=(0, 5))
        ttk.Button(proxy_toolbar, text="➕ 添加", command=self._add_proxy, width=10).pack(side=tk.LEFT, padx=2)
        ttk.Button(proxy_toolbar, text="✏ 编辑", command=self._edit_proxy, width=10).pack(side=tk.LEFT, padx=2)
        ttk.Button(proxy_toolbar, text="🗑 删除", command=self._del_proxy, width=10).pack(side=tk.LEFT, padx=2)

        self.proxy_tree = ttk.Treeview(proxy_tab, columns=("name", "type", "local", "remote"),
                                        show="headings")
        self.proxy_tree.heading("name", text="名称")
        self.proxy_tree.heading("type", text="类型")
        self.proxy_tree.heading("local", text="本地地址")
        self.proxy_tree.heading("remote", text="远程端口")
        self.proxy_tree.column("name", width=120)
        self.proxy_tree.column("type", width=70)
        self.proxy_tree.column("local", width=220)
        self.proxy_tree.column("remote", width=100)
        self.proxy_tree.pack(fill=tk.BOTH, expand=True)

        # ---- 访问者标签页 ----
        visitor_tab = ttk.Frame(notebook)
        notebook.add(visitor_tab, text="👥 XTCP 访问者")

        vis_toolbar = ttk.Frame(visitor_tab)
        vis_toolbar.pack(fill=tk.X, pady=(0, 5))
        ttk.Button(vis_toolbar, text="➕ 添加", command=self._add_visitor, width=10).pack(side=tk.LEFT, padx=2)
        ttk.Button(vis_toolbar, text="✏ 编辑", command=self._edit_visitor, width=10).pack(side=tk.LEFT, padx=2)
        ttk.Button(vis_toolbar, text="🗑 删除", command=self._del_visitor, width=10).pack(side=tk.LEFT, padx=2)

        self.visitor_tree = ttk.Treeview(visitor_tab, columns=("name", "target", "bind"),
                                          show="headings")
        self.visitor_tree.heading("name", text="名称")
        self.visitor_tree.heading("target", text="目标代理")
        self.visitor_tree.heading("bind", text="绑定端口")
        self.visitor_tree.column("name", width=150)
        self.visitor_tree.column("target", width=150)
        self.visitor_tree.column("bind", width=120)
        self.visitor_tree.pack(fill=tk.BOTH, expand=True)

        # ---- 日志标签页 ----
        log_tab = ttk.Frame(notebook)
        notebook.add(log_tab, text="📜 运行日志")

        self.log_text = scrolledtext.ScrolledText(log_tab, wrap=tk.WORD,
                                                   font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4")
        self.log_text.pack(fill=tk.BOTH, expand=True)
        self.log_text.configure(state=tk.DISABLED)

        self.log_text.tag_configure("WARNING", foreground="#e5c07b")
        self.log_text.tag_configure("ERROR", foreground="#e06c75")
        self.log_text.tag_configure("INFO", foreground="#98c379")

    # ==================== 代理编辑 ====================

    def _add_proxy(self):
        dlg = ProxyEditDialog(self.root, "添加代理", self.PROXY_FIELDS)
        if dlg.result:
            # 解析 custom_domains（逗号分隔）
            custom_domains_str = dlg.result.get("custom_domains", "")
            custom_domains = [d.strip() for d in custom_domains_str.split(",") if d.strip()] if custom_domains_str else []

            cfg = {
                "name": dlg.result["name"],
                "type": dlg.result["type"],
                "local_ip": dlg.result["local_ip"],
                "local_port": int(dlg.result["local_port"]) if dlg.result["local_port"] else 0,
                "remote_port": int(dlg.result["remote_port"]) if dlg.result["remote_port"] else 0,
                "secret_key": dlg.result.get("secret_key", ""),
            }
            # HTTP/HTTPS 代理额外字段
            if dlg.result["type"] in ("http", "https"):
                cfg["custom_domains"] = custom_domains
                cfg["subdomain"] = dlg.result.get("subdomain", "")
                cfg["http_user"] = dlg.result.get("http_user", "")
                cfg["http_pwd"] = dlg.result.get("http_pwd", "")
                cfg["host_header_rewrite"] = ""

            self._proxies.append(cfg)
            self._refresh_proxy_tree()
            self._log_message(f"添加代理: {cfg['name']} ({cfg['type']})", "INFO")

    def _edit_proxy(self):
        sel = self.proxy_tree.selection()
        if not sel:
            messagebox.showwarning("提示", "请先选择要编辑的代理")
            return
        idx = self.proxy_tree.index(sel[0])
        old_cfg = self._proxies[idx]
        # 将 custom_domains 列表转为逗号分隔字符串用于编辑
        initial = {}
        for k, v in old_cfg.items():
            if k == "custom_domains" and isinstance(v, list):
                initial[k] = ", ".join(v)
            else:
                initial[k] = str(v)
        dlg = ProxyEditDialog(self.root, "编辑代理", self.PROXY_FIELDS, initial)
        if dlg.result:
            custom_domains_str = dlg.result.get("custom_domains", "")
            custom_domains = [d.strip() for d in custom_domains_str.split(",") if d.strip()] if custom_domains_str else []

            new_cfg = {
                "name": dlg.result["name"],
                "type": dlg.result["type"],
                "local_ip": dlg.result["local_ip"],
                "local_port": int(dlg.result["local_port"]) if dlg.result["local_port"] else 0,
                "remote_port": int(dlg.result["remote_port"]) if dlg.result["remote_port"] else 0,
                "secret_key": dlg.result.get("secret_key", ""),
            }
            if dlg.result["type"] in ("http", "https"):
                new_cfg["custom_domains"] = custom_domains
                new_cfg["subdomain"] = dlg.result.get("subdomain", "")
                new_cfg["http_user"] = dlg.result.get("http_user", "")
                new_cfg["http_pwd"] = dlg.result.get("http_pwd", "")
                new_cfg["host_header_rewrite"] = ""

            self._proxies[idx] = new_cfg
            self._refresh_proxy_tree()
            self._log_message(f"更新代理: {new_cfg['name']}", "INFO")

    def _del_proxy(self):
        sel = self.proxy_tree.selection()
        if not sel:
            return
        idx = self.proxy_tree.index(sel[0])
        name = self._proxies[idx]["name"]
        if messagebox.askyesno("确认", f"确定删除代理 '{name}'?"):
            del self._proxies[idx]
            self._refresh_proxy_tree()
            self._log_message(f"删除代理: {name}", "INFO")

    def _refresh_proxy_tree(self):
        for item in self.proxy_tree.get_children():
            self.proxy_tree.delete(item)
        for p in self._proxies:
            local = f"{p['local_ip']}:{p['local_port']}"
            ptype = p["type"]
            if ptype in ("http", "https"):
                domains = p.get("custom_domains", [])
                remote = ", ".join(domains) if domains else p.get("subdomain", "")
            else:
                remote = str(p.get("remote_port", ""))
            self.proxy_tree.insert("", tk.END,
                                    values=(p["name"], ptype, local, remote))
        self.proxies_count_var.set(str(len(self._proxies)))

    # ==================== 访问者编辑 ====================

    def _add_visitor(self):
        dlg = ProxyEditDialog(self.root, "添加 XTCP 访问者", self.VISITOR_FIELDS)
        if dlg.result:
            cfg = {
                "name": dlg.result["name"],
                "type": "xtcp",
                "server_name": dlg.result["server_name"],
                "secret_key": dlg.result["secret_key"],
                "bind_port": int(dlg.result["bind_port"]),
                "local_ip": dlg.result["local_ip"],
            }
            self._visitors.append(cfg)
            self._refresh_visitor_tree()
            self._log_message(f"添加访问者: {cfg['name']}", "INFO")

    def _edit_visitor(self):
        sel = self.visitor_tree.selection()
        if not sel:
            messagebox.showwarning("提示", "请先选择要编辑的访问者")
            return
        idx = self.visitor_tree.index(sel[0])
        old_cfg = self._visitors[idx]
        initial = {k: str(v) for k, v in old_cfg.items()}
        dlg = ProxyEditDialog(self.root, "编辑访问者", self.VISITOR_FIELDS, initial)
        if dlg.result:
            new_cfg = {
                "name": dlg.result["name"],
                "type": "xtcp",
                "server_name": dlg.result["server_name"],
                "secret_key": dlg.result["secret_key"],
                "bind_port": int(dlg.result["bind_port"]),
                "local_ip": dlg.result["local_ip"],
            }
            self._visitors[idx] = new_cfg
            self._refresh_visitor_tree()
            self._log_message(f"更新访问者: {new_cfg['name']}", "INFO")

    def _del_visitor(self):
        sel = self.visitor_tree.selection()
        if not sel:
            return
        idx = self.visitor_tree.index(sel[0])
        name = self._visitors[idx]["name"]
        if messagebox.askyesno("确认", f"确定删除访问者 '{name}'?"):
            del self._visitors[idx]
            self._refresh_visitor_tree()
            self._log_message(f"删除访问者: {name}", "INFO")

    def _refresh_visitor_tree(self):
        for item in self.visitor_tree.get_children():
            self.visitor_tree.delete(item)
        for v in self._visitors:
            self.visitor_tree.insert("", tk.END,
                                      values=(v["name"], v["server_name"], v["bind_port"]))

    # ==================== 连接管理 ====================

    def _connect(self):
        if not self._engine_running:
            self.engine.run_in_thread()
            self._engine_running = True

        addr = self.server_addr_var.get().strip()
        try:
            port = int(self.server_port_var.get())
        except ValueError:
            messagebox.showerror("错误", "端口号必须为数字")
            return
        token = self.token_var.get().strip()
        try:
            pool = int(self.pool_var.get())
        except ValueError:
            pool = 5

        if not addr or not token:
            messagebox.showerror("错误", "服务器地址和 Token 不能为空")
            return

        self.connect_btn.configure(state=tk.DISABLED)
        self._log_message(f"正在连接 {addr}:{port} ...", "INFO")

        def _async_connect():
            session = ControlSession(addr, port, token, pool)
            self.engine.session = session

            async def _connect_and_run():
                ok = await session.connect()
                if not ok:
                    self.root.after(0, self._on_connect_failed, "登录失败，检查 Token")
                    return
                self.root.after(0, self._on_connected, session)
                try:
                    await session.run(self._proxies, self._visitors)
                except Exception as e:
                    self.root.after(0, self._log_message, f"会话异常: {e}", "ERROR")
                finally:
                    self.root.after(0, self._on_disconnected)

            self.engine.submit(_connect_and_run())

        self.root.after(300, _async_connect)

    def _on_connected(self, session):
        self._connected = True
        self._connect_start_time = time.time()
        self.status_var.set("🟢 已连接")
        self.status_label.configure(fg="#27ae60")
        self.connect_btn.configure(state=tk.DISABLED)
        self.disconnect_btn.configure(state=tk.NORMAL)
        self.run_id_var.set(session.run_id or "--")
        self._log_message("连接成功!", "INFO")
        self._start_refresh()

    def _on_connect_failed(self, msg: str):
        self._log_message(msg, "ERROR")
        self.connect_btn.configure(state=tk.NORMAL)
        self.status_var.set("❌ 连接失败")
        self.status_label.configure(fg="#e74c3c")

    def _on_disconnected(self):
        self._connected = False
        self._stop_refresh()
        self.status_var.set("⏸ 已断开")
        self.status_label.configure(fg="#e67e22")
        self.connect_btn.configure(state=tk.NORMAL)
        self.disconnect_btn.configure(state=tk.DISABLED)
        self.run_id_var.set("--")
        self.uptime_var.set("--")
        self._log_message("连接已断开，可重新连接", "WARNING")

    def _disconnect(self):
        if self.engine.session:
            self.engine.session.disconnect()
        self._connected = False
        self._on_disconnected()

    # ==================== 日志 ====================

    def _poll_logs(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self._append_log(msg)
        except queue.Empty:
            pass
        if self._connected or self._refresh_id:
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
        formatted = f"{time.strftime('%H:%M:%S')} [{level}] {msg}"
        self._append_log(formatted)

    def _start_refresh(self):
        self._poll_logs()
        self._refresh_status()
        self._refresh_proxy_tree()
        self._refresh_visitor_tree()

    def _stop_refresh(self):
        if self._refresh_id:
            self.root.after_cancel(self._refresh_id)
            self._refresh_id = None

    def _refresh_status(self):
        if not self._connected:
            return
        if self._connect_start_time:
            elapsed = int(time.time() - self._connect_start_time)
            h, m, s = elapsed // 3600, (elapsed % 3600) // 60, elapsed % 60
            self.uptime_var.set(f"{h:02d}:{m:02d}:{s:02d}")
        self._refresh_id = self.root.after(2000, self._refresh_status)

    # ==================== 配置导入导出 ====================

    def _save_config(self):
        config = {
            "server": {
                "addr": self.server_addr_var.get(),
                "port": int(self.server_port_var.get()),
                "token": self.token_var.get(),
                "pool_count": int(self.pool_var.get()),
            },
            "proxies": self._proxies,
            "visitors": self._visitors,
        }
        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON files", "*.json")],
            initialfile="frpc_config.json",
        )
        if path:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
            self._log_message(f"配置已导出: {path}", "INFO")

    def _load_config(self):
        path = filedialog.askopenfilename(filetypes=[("JSON files", "*.json")])
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                config = json.load(f)

            server = config.get("server", {})
            self.server_addr_var.set(server.get("addr", "127.0.0.1"))
            self.server_port_var.set(str(server.get("port", 7000)))
            self.token_var.set(server.get("token", ""))
            self.pool_var.set(str(server.get("pool_count", 5)))

            self._proxies = config.get("proxies", [])
            self._visitors = config.get("visitors", [])
            self._refresh_proxy_tree()
            self._refresh_visitor_tree()
            self._log_message(f"配置已导入: {path} (代理:{len(self._proxies)} 访问者:{len(self._visitors)})", "INFO")
        except Exception as e:
            self._log_message(f"导入配置失败: {e}", "ERROR")

    # ==================== 关闭与托盘 ====================

    def _on_close(self):
        """点击关闭按钮时弹出选择对话框"""
        dialog = tk.Toplevel(self.root)
        dialog.title("关闭确认")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.configure(bg="#f0f0f0")

        # 居中显示
        dialog.update_idletasks()
        dw, dh = 320, 160
        x = self.root.winfo_x() + (self.root.winfo_width() - dw) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - dh) // 2
        dialog.geometry(f"{dw}x{dh}+{x}+{y}")

        tk.Label(dialog, text="请选择关闭方式：",
                 font=("Microsoft YaHei", 11), bg="#f0f0f0").pack(pady=(25, 20))

        btn_frame = ttk.Frame(dialog)
        btn_frame.pack()

        def minimize():
            dialog.destroy()
            self._minimize_to_tray()

        def exit_app():
            dialog.destroy()
            self._exit_app()

        ttk.Button(btn_frame, text="最小化到托盘",
                   command=minimize, width=14).pack(side=tk.LEFT, padx=10)
        ttk.Button(btn_frame, text="直接退出",
                   command=exit_app, width=14).pack(side=tk.LEFT, padx=10)

        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)

    def _minimize_to_tray(self):
        """最小化到系统托盘"""
        self._tray.show()
        self.root.withdraw()
        self._log_message("已最小化到系统托盘", "INFO")

    def _restore_from_tray(self):
        """从系统托盘恢复窗口"""
        self._tray.hide()
        self.root.deiconify()
        self.root.state("normal")

    def _exit_app(self):
        """退出应用"""
        if self._connected:
            self._disconnect()
        self._tray.destroy()
        self.engine.stop()
        self.root.destroy()


def main():
    root = tk.Tk()
    app = ClientGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()