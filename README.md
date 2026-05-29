<p align="center">
  <h1 align="center">FRP Service Manager</h1>
  <p align="center">
    基于 <a href="https://github.com/fatedier/frp">frp</a> 二次开发的轻量级内网穿透服务管理工具
    <br />
    <a href="https://github.com/financia0x00001/frp-service-manager/releases/latest"><strong>⬇ 下载最新版本</strong></a>
  </p>
</p>

---

## 项目简介

FRP Service Manager 是基于 [fatedier/frp](https://github.com/fatedier/frp) 核心协议二次开发的内网穿透服务管理工具。项目使用 Python 从零实现了 frp 的通信协议，并在此基础上提供了 **Windows 系统服务管理** 和 **图形化操作界面**，让内网穿透的部署和使用变得简单直观。

### 为什么做这个项目？

原版 frp 是优秀的内网穿透工具，但在 Windows 环境下使用存在一些不便：
- 需要命令行操作，对非技术用户不友好
- 无法直接注册为 Windows 系统服务，开机不能自动启动
- 缺少可视化的配置管理界面

本项目正是为了解决这些痛点而生。

---

## 特性

- 🖥️ **图形化管理界面** — 基于 tkinter，无需命令行操作
- 🔧 **Windows 服务模式** — 一键注册为系统服务，开机自动启动
- 🔐 **Token 认证** — 支持随机生成 Token，保障通信安全
- 📡 **多协议代理** — 支持 TCP / UDP / XTCP（P2P 打洞）代理
- 📋 **实时状态监控** — 服务运行状态、连接数、日志实时查看
- 📦 **单文件分发** — 打包为独立 EXE，无需安装 Python 环境
- 🚀 **轻量高效** — 纯 Python 实现 frp 协议，无额外依赖

---

## 下载安装

前往 [Releases 页面](https://github.com/financia0x00001/frp-service-manager/releases/latest) 下载最新版本。

| 文件 | 说明 |
|------|------|
| `frps_service_manager.exe` | 服务端服务管理器（需管理员权限） |
| `frpc_lite_gui.exe` | 客户端图形界面 |

---

## 使用说明

### 服务端（frps_service_manager.exe）

1. **以管理员身份运行** `frps_service_manager.exe`
2. 在「服务配置」面板中设置：
   - **绑定地址**：默认 `0.0.0.0`（监听所有网卡）
   - **绑定端口**：默认 `7000`
   - **Token**：点击「随机生成」或手动输入
3. 点击「安装服务」→ 将程序注册为 Windows 系统服务
4. 点击「启动服务」→ 服务在后台运行，开机自动启动
5. 修改配置后点击「应用配置并重启服务」

> ⚠️ 服务管理操作需要管理员权限，请右键选择「以管理员身份运行」

### 客户端（frpc_lite_gui.exe）

1. 运行 `frpc_lite_gui.exe`
2. 填写服务端连接信息：
   - **服务器地址**：FRP 服务端的 IP 或域名
   - **服务器端口**：默认 `7000`
   - **Token**：与服务端配置一致
3. 添加代理规则（支持 TCP / UDP / XTCP）
4. 点击「连接」开始内网穿透

### 代理配置示例

```json
{
  "proxies": [
    {
      "name": "ssh",
      "type": "tcp",
      "local_ip": "127.0.0.1",
      "local_port": 22,
      "remote_port": 6000
    },
    {
      "name": "web",
      "type": "tcp",
      "local_ip": "127.0.0.1",
      "local_port": 8080,
      "remote_port": 8080
    },
    {
      "name": "rdp_p2p",
      "type": "xtcp",
      "local_ip": "127.0.0.1",
      "local_port": 3389,
      "secret_key": "your_shared_secret"
    }
  ],
  "visitors": [
    {
      "name": "rdp_p2p_visitor",
      "type": "xtcp",
      "server_name": "rdp_p2p",
      "secret_key": "your_shared_secret",
      "bind_port": 13389
    }
  ]
}
```

---

## 项目结构

```
├── frp_lite_protocol.py        # frp 通信协议实现
├── frps_lite.py                # 服务端核心逻辑
├── frpc_lite.py                # 客户端核心逻辑
├── frps_lite_gui.py            # 服务端图形界面
├── frpc_lite_gui.py            # 客户端图形界面
├── frps_service_manager.py     # 服务端服务管理器（Windows 服务）
├── frpc_service_manager.py     # 客户端服务管理器（Windows 服务）
├── frps_service.py             # Windows 系统服务模块
├── install_service.py          # 服务安装脚本
├── build_exe.bat               # 一键打包脚本
├── frps_service_config.json    # 服务端配置文件
├── frpc_config.json            # 客户端配置示例
└── .gitignore
```

---

## 系统要求

- **操作系统**：Windows 7 / 8 / 10 / 11（64位）
- **权限要求**：服务端管理器需要管理员权限
- **网络要求**：服务端需要开放对应端口（默认 7000）

---

## 常见问题

**Q: 安装服务时提示权限不足？**
A: 请右键程序选择「以管理员身份运行」。

**Q: 服务启动后状态显示已停止？**
A: 检查端口是否被占用，查看日志文件 `frps_service.log` 获取详细错误信息。

**Q: 客户端无法连接服务端？**
A: 1) 确认服务端已启动；2) 检查防火墙是否放行端口；3) 确认 Token 配置一致。

**Q: 如何卸载服务？**
A: 以管理员身份运行服务端管理器，点击「卸载服务」即可。

---

## 致谢

本项目基于 [fatedier/frp](https://github.com/fatedier/frp) 的通信协议进行二次开发，感谢 frp 开源社区的贡献。

---

## 开源协议

本项目基于 [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0) 开源，与上游 [frp](https://github.com/fatedier/frp) 项目保持一致。

本项目仅供学习和个人使用，请遵守相关法律法规，不得用于非法用途。使用者需自行承担使用风险，开发者不对任何因使用本软件造成的损失负责。
