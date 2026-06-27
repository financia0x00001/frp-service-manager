"""
frp-lite 客户端 (frpc_lite)
============================
轻量级内网穿透客户端，运行在内网机器上，将本地服务暴露到公网。

功能:
    - 连接服务端，建立控制通道
    - 工作连接池（预建立，减少延迟）
    - TCP 端口映射（将公网端口流量转发到本地服务）
    - UDP 数据包转发
    - XTCP NAT 穿透（P2P 直连）
    - 自动重连 + 心跳维持
    - 支持多个代理同时运行

配置方式（命令行 + JSON 配置文件）:
    python frpc_lite.py --config config.json

JSON 配置格式:
{
    "server": {"addr": "your-server.com", "port": 7000, "token": "mysecret"},
    "proxies": [
        {"name": "web", "type": "tcp", "local_ip": "127.0.0.1", "local_port": 8080, "remote_port": 8080},
        {"name": "ssh", "type": "tcp", "local_ip": "127.0.0.1", "local_port": 22, "remote_port": 2222},
        {"name": "dns", "type": "udp", "local_ip": "127.0.0.1", "local_port": 53, "remote_port": 5353},
        {"name": "p2p", "type": "xtcp", "local_ip": "127.0.0.1", "local_port": 3389, "secret_key": "shared_secret"}
    ]
}
"""

import asyncio
import argparse
import base64
import json
import logging
import os
import signal
import socket
import sys
import time
from typing import Dict, List, Optional, Tuple

from frp_lite_protocol import (
    MSG_LOGIN, MSG_LOGIN_RESP, MSG_NEW_PROXY, MSG_NEW_PROXY_RESP,
    MSG_CLOSE_PROXY, MSG_REQ_WORK_CONN, MSG_NEW_WORK_CONN,
    MSG_START_WORK_CONN, MSG_PING, MSG_PONG, MSG_UDP_PACKET,
    MSG_NAT_HOLE_VISITOR, MSG_NAT_HOLE_CLIENT,
    MSG_NAT_HOLE_RESP, MSG_NAT_HOLE_SID,
    PROXY_TCP, PROXY_UDP, PROXY_XTCP, PROXY_HTTP, PROXY_HTTPS,
    read_message, write_message, generate_run_id,
    generate_transaction_id, generate_sid,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("frpc")


def now_ts() -> float:
    return time.time()


# ============================================================
#  TCP 代理客户端
# ============================================================
class TCPProxyClient:
    """客户端 TCP 代理：接收工作连接，转发到本地服务"""

    def __init__(self, cfg: dict, control: "ControlSession"):
        self.name = cfg["name"]
        self.local_ip = cfg.get("local_ip", "127.0.0.1")
        self.local_port = cfg["local_port"]
        self.control = control

    async def handle_work_conn(self, reader: asyncio.StreamReader,
                                writer: asyncio.StreamWriter):
        """处理来自服务端的工作连接"""
        try:
            local_reader, local_writer = await asyncio.open_connection(
                self.local_ip, self.local_port
            )
        except Exception as e:
            log.error(f"[TCP:{self.name}] 连接本地服务 {self.local_ip}:{self.local_port} 失败: {e}")
            writer.close()
            return

        try:
            await asyncio.gather(
                _pipe(local_reader, writer),
                _pipe(reader, local_writer),
            )
        except Exception as e:
            log.debug(f"[TCP:{self.name}] 转发错误: {e}")
        finally:
            try:
                writer.close()
            except Exception:
                pass
            try:
                local_writer.close()
            except Exception:
                pass


# ============================================================
#  UDP 代理客户端
# ============================================================
class UDPProxyClient:
    """客户端 UDP 代理：监听本地请求，转发到服务端再转到公网用户"""

    def __init__(self, cfg: dict, control: "ControlSession"):
        self.name = cfg["name"]
        self.local_ip = cfg.get("local_ip", "127.0.0.1")
        self.local_port = cfg["local_port"]
        self.control = control
        self._transport: Optional[asyncio.DatagramTransport] = None
        # 跟踪每个远程客户端的地址，用于回包
        self._remote_addrs: Dict[Tuple, Tuple] = {}
        # 本地服务地址缓存（首次连接后记录）
        self._local_service_addr: Optional[Tuple] = None
        self._local_service_transport: Optional[asyncio.DatagramTransport] = None
        self._local_protocol: Optional["_UDPClientLocalProtocol"] = None
        self._pending_queue: Dict[Tuple, List[Tuple[bytes, Tuple]]] = {}

    async def start(self):
        loop = asyncio.get_event_loop()

        # 监听本地端口，接收内网客户端请求
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.local_ip, self.local_port))
        sock.setblocking(False)

        self._local_protocol = _UDPClientLocalProtocol(self)
        await loop.create_datagram_endpoint(
            lambda: self._local_protocol,
            sock=sock,
        )
        log.info(f"[UDP:{self.name}] 监听本地 UDP {self.local_ip}:{self.local_port}")

    async def send_to_server(self, data_b64: str, src_addr: str, src_port: int):
        """将 UDP 包发送给服务端"""
        await self.control.send_msg(MSG_UDP_PACKET, {
            "proxy_name": self.name,
            "content_b64": data_b64,
            "src_addr": src_addr,
            "src_port": src_port,
            "local_addr": self.local_ip,
            "local_port": self.local_port,
        })

    async def handle_server_packet(self, data: dict):
        """处理来自服务端的 UDP 包（来自公网用户的请求）"""
        content_b64 = data.get("content_b64", "")
        src_addr = data.get("src_addr", "")
        src_port = data.get("src_port", 0)

        if not content_b64:
            return

        raw_data = base64.b64decode(content_b64)
        remote_addr = (src_addr, src_port)

        if self._local_service_transport:
            self._local_service_transport.sendto(raw_data, self._local_service_addr)
        else:
            if remote_addr not in self._pending_queue:
                self._pending_queue[remote_addr] = []
            self._pending_queue[remote_addr].append((raw_data, remote_addr))

    async def close(self):
        if self._local_service_transport:
            self._local_service_transport.close()
        if hasattr(self, "_transport") and self._transport:
            self._transport.close()
        log.info(f"[UDP:{self.name}] 已关闭")


class _UDPClientLocalProtocol(asyncio.DatagramProtocol):
    def __init__(self, proxy: UDPProxyClient):
        self.proxy = proxy

    def connection_made(self, transport):
        self.proxy._local_service_transport = transport

    def datagram_received(self, data: bytes, addr: Tuple):
        content_b64 = base64.b64encode(data).decode()
        asyncio.create_task(
            self.proxy.send_to_server(content_b64, addr[0], addr[1])
        )

    def error_received(self, exc):
        log.debug(f"[UDP:{self.proxy.name}] 本地错误: {exc}")

    def connection_lost(self, exc):
        pass


# ============================================================
#  XTCP P2P 代理客户端
# ============================================================
class XTCPProxyClient:
    """客户端 XTCP 代理：参与 NAT 穿透，尝试与访问者建立 P2P 直连"""

    def __init__(self, cfg: dict, control: "ControlSession"):
        self.name = cfg["name"]
        self.local_ip = cfg.get("local_ip", "127.0.0.1")
        self.local_port = cfg["local_port"]
        self.secret_key = cfg.get("secret_key", "")
        self.control = control
        self._p2p_sock: Optional[socket.socket] = None

    async def handle_nat_hole_client(self, data: dict):
        """处理来自服务端的 NAT 穿透请求（我们是代理持有者）"""
        transaction_id = data.get("transaction_id", "")
        sid = data.get("sid", "")
        visitor_addrs = data.get("mapped_addrs", [])

        log.info(f"[XTCP:{self.name}] 收到 NAT 穿透请求 sid={sid} visit_addr={visitor_addrs}")

        # 准备本地 UDP socket 用于 P2P 直连
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", 0))
        sock.setblocking(False)
        local_addr = sock.getsockname()

        # 获取本机公网映射地址
        mapped_addrs = [f"{self.control.server_addr}:{local_addr[1]}"]

        # 回复服务端
        await self.control.send_msg(MSG_NAT_HOLE_RESP, {
            "transaction_id": transaction_id,
            "proxy_name": self.name,
            "mapped_addrs": mapped_addrs,
            "sid": sid,
        })

        # 尝试向访问者地址发送打洞包
        for addr_str in visitor_addrs:
            try:
                host, port = addr_str.rsplit(":", 1)
                port = int(port)
                hole_msg = f"P2P_HOLE:{sid}".encode()
                sock.sendto(hole_msg, (host, port))
                log.info(f"[XTCP:{self.name}] 发送打洞包到 {host}:{port}")
            except Exception as e:
                log.debug(f"[XTCP:{self.name}] 打洞失败: {e}")

        # 等待 P2P 连接建立
        loop = asyncio.get_event_loop()
        try:
            data, addr = await _recvfrom_timeout(loop, sock, 512, 10.0)
            log.info(f"[XTCP:{self.name}] P2P 连接建立成功! 来自 {addr}")

            # P2P 连接已建立，通过这个 UDP socket 转发数据到本地服务
            asyncio.create_task(self._handle_p2p_conn(sock, addr))
        except asyncio.TimeoutError:
            log.warning(f"[XTCP:{self.name}] P2P 连接超时，使用服务端中继")
            sock.close()

    async def _handle_p2p_conn(self, sock: socket.socket, visitor_addr: Tuple):
        """处理 P2P 直连：将 UDP 数据转发到本地 TCP 服务"""
        log.info(f"[XTCP:{self.name}] P2P 直连模式 - 转发到 {self.local_ip}:{self.local_port}")

        try:
            local_reader, local_writer = await asyncio.open_connection(
                self.local_ip, self.local_port
            )
        except Exception as e:
            log.error(f"[XTCP:{self.name}] 连接本地服务失败: {e}")
            sock.close()
            return

        loop = asyncio.get_event_loop()
        running = True

        async def udp_to_local():
            nonlocal running
            try:
                while running:
                    data, _ = await _recvfrom_timeout(loop, sock, 65536, 30.0)
                    if data:
                        local_writer.write(data)
                        await local_writer.drain()
            except asyncio.TimeoutError:
                pass
            except Exception:
                pass
            finally:
                running = False

        async def local_to_udp():
            nonlocal running
            try:
                while running:
                    data = await local_reader.read(65536)
                    if not data:
                        break
                    await loop.sock_sendall(sock, data, visitor_addr)
            except Exception:
                pass
            finally:
                running = False

        await asyncio.gather(udp_to_local(), local_to_udp())
        sock.close()
        try:
            local_writer.close()
        except Exception:
            pass


# ============================================================
#  XTCP 访问者客户端（发起 P2P 连接的一方）
# ============================================================
class XTCPVisitorClient:
    """客户端 XTCP 访问者：发起 NAT 穿透请求，尝试与被访问者建立 P2P 直连"""

    def __init__(self, cfg: dict, control: "ControlSession"):
        self.name = cfg["name"]
        self.local_ip = cfg.get("local_ip", "0.0.0.0")
        self.local_port = cfg.get("local_port", 0)
        self.server_name = cfg.get("server_name", cfg["name"])
        self.secret_key = cfg.get("secret_key", "")
        self.bind_port = cfg.get("bind_port", 0)
        self.control = control
        self._server: Optional[asyncio.AbstractServer] = None

    async def start(self):
        """启动本地监听，当有本地连接时发起 P2P 穿透"""
        try:
            self._server = await asyncio.start_server(
                self._handle_local_conn, self.local_ip, self.bind_port
            )
            log.info(f"[XTCP-V:{self.name}] 本地监听 {self.local_ip}:{self.bind_port}")
        except OSError as e:
            log.error(f"[XTCP-V:{self.name}] 监听失败: {e}")
            raise

    async def _handle_local_conn(self, reader: asyncio.StreamReader,
                                  writer: asyncio.StreamWriter):
        """本地用户请求接入 → 发起 NAT 穿透"""
        peername = writer.get_extra_info("peername")
        log.info(f"[XTCP-V:{self.name}] 本地用户连接 {peername}，开始 NAT 穿透")

        # 1. PreCheck
        transaction_id = generate_transaction_id()
        await self.control.send_msg(MSG_NAT_HOLE_VISITOR, {
            "transaction_id": transaction_id,
            "proxy_name": self.server_name,
            "pre_check": True,
        })

        # 等待 precheck 响应
        resp = await self.control.wait_for_resp(transaction_id, timeout=10.0)
        if resp is None or resp.get("error"):
            log.warning(f"[XTCP-V:{self.name}] PreCheck 失败: {resp}")
            writer.close()
            return

        # 2. 正式请求 NAT 穿透
        transaction_id2 = generate_transaction_id()
        await self.control.send_msg(MSG_NAT_HOLE_VISITOR, {
            "transaction_id": transaction_id2,
            "proxy_name": self.server_name,
            "pre_check": False,
        })

        resp = await self.control.wait_for_resp(transaction_id2, timeout=20.0)
        if resp is None or resp.get("error"):
            log.warning(f"[XTCP-V:{self.name}] NAT 穿透失败: {resp}")
            writer.close()
            return

        sid = resp.get("sid", "")
        client_addrs = resp.get("client_mapped_addrs", [])
        log.info(f"[XTCP-V:{self.name}] 穿透响应 sid={sid} target={client_addrs}")

        # 3. 尝试 P2P 直连
        p2p_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        p2p_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        p2p_sock.bind(("0.0.0.0", 0))
        p2p_sock.setblocking(False)

        connected = False
        for addr_str in client_addrs:
            try:
                host, port = addr_str.rsplit(":", 1)
                port = int(port)
                hole_msg = f"P2P_HOLE:{sid}".encode()
                p2p_sock.sendto(hole_msg, (host, port))
            except Exception:
                continue

        # 等待 P2P 响应
        loop = asyncio.get_event_loop()
        try:
            data, p2p_addr = await _recvfrom_timeout(loop, p2p_sock, 512, 8.0)
            log.info(f"[XTCP-V:{self.name}] P2P 连接建立! 来自 {p2p_addr}")
            connected = True
        except asyncio.TimeoutError:
            log.warning(f"[XTCP-V:{self.name}] P2P 超时，回退到服务端中继")
            p2p_sock.close()
            writer.close()
            return

        if connected:
            # P2P 连接已建立，在本地用户和 P2P socket 之间转发
            local_reader, local_writer = reader, writer
            running = True

            async def local_to_p2p():
                nonlocal running
                try:
                    while running:
                        data = await local_reader.read(65536)
                        if not data:
                            break
                        await loop.sock_sendall(p2p_sock, data, p2p_addr)
                except Exception:
                    pass
                finally:
                    running = False

            async def p2p_to_local():
                nonlocal running
                try:
                    while running:
                        data, _ = await _recvfrom_timeout(loop, p2p_sock, 65536, 30.0)
                        if data:
                            local_writer.write(data)
                            await local_writer.drain()
                except asyncio.TimeoutError:
                    pass
                except Exception:
                    pass
                finally:
                    running = False

            await asyncio.gather(local_to_p2p(), p2p_to_local())
            p2p_sock.close()
            try:
                local_writer.close()
            except Exception:
                pass

    async def close(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        log.info(f"[XTCP-V:{self.name}] 已关闭")


# ============================================================
#  控制会话（管理客户端与服务端的通信）
# ============================================================
class ControlSession:
    """客户端控制会话：维护与服务端的连接，管理代理注册和心跳"""

    def __init__(self, server_addr: str, server_port: int, token: str,
                 pool_count: int = 5):
        self.server_addr = server_addr
        self.server_port = server_port
        self.token = token
        self.pool_count = pool_count
        self.run_id = ""
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._alive = False
        self._pending_resp: Dict[str, asyncio.Future] = {}
        self._work_conn_tasks: List[asyncio.Task] = []
        self._proxy_clients: Dict[str, object] = {}
        self._visitor_clients: Dict[str, XTCPVisitorClient] = {}
        self._send_lock = asyncio.Lock()

    async def send_msg(self, msg_type: str, data: dict = None):
        """发送消息到服务端"""
        if self._writer:
            async with self._send_lock:
                await write_message(self._writer, msg_type, data)

    async def wait_for_resp(self, transaction_id: str, timeout: float = 10.0) -> Optional[dict]:
        """等待特定 transaction_id 的响应"""
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_resp[transaction_id] = future
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self._pending_resp.pop(transaction_id, None)

    async def connect(self) -> bool:
        """连接到服务端并登录"""
        try:
            self._reader, self._writer = await asyncio.open_connection(
                self.server_addr, self.server_port
            )
        except Exception as e:
            log.error(f"连接服务端失败 {self.server_addr}:{self.server_port}: {e}")
            return False

        await write_message(self._writer, MSG_LOGIN, {
            "version": "1.0",
            "hostname": socket.gethostname(),
            "token": self.token,
            "pool_count": self.pool_count,
        })

        msg = await read_message(self._reader)
        if msg is None or msg.type != MSG_LOGIN_RESP:
            log.error("登录响应异常")
            return False

        if not msg.data.get("ok"):
            log.error(f"登录失败: {msg.data.get('error', 'unknown')}")
            return False

        self.run_id = msg.data.get("run_id", "")
        log.info(f"登录成功 (run_id={self.run_id})")
        self._alive = True
        return True

    async def run(self, proxies: List[dict], visitors: List[dict]):
        """主循环：注册代理 → 处理消息 → 维持心跳"""
        if not self._alive:
            return

        # 注册所有代理
        for pxy_cfg in proxies:
            await self._register_proxy(pxy_cfg)

        # 注册访问者
        for vis_cfg in visitors:
            await self._register_visitor(vis_cfg)

        # 启动心跳
        heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        # 消息处理循环
        try:
            while self._alive:
                msg = await read_message(self._reader)
                if msg is None:
                    log.warning("与服务端的连接断开")
                    break
                await self._dispatch(msg)
        except Exception as e:
            log.debug(f"控制会话异常: {e}")
        finally:
            self._alive = False
            heartbeat_task.cancel()
            await self._cleanup()

    async def _dispatch(self, msg):
        """消息分发"""
        handlers = {
            MSG_REQ_WORK_CONN: self._handle_req_work_conn,
            MSG_NEW_PROXY_RESP: self._handle_new_proxy_resp,
            MSG_PONG: self._handle_pong,
            MSG_UDP_PACKET: self._handle_udp_packet,
            MSG_NAT_HOLE_CLIENT: self._handle_nat_hole_client,
            MSG_NAT_HOLE_RESP: self._handle_nat_hole_resp,
        }
        handler = handlers.get(msg.type)
        if handler:
            try:
                await handler(msg.data)
            except Exception as e:
                log.error(f"处理 {msg.type} 异常: {e}")

    # ---- 代理注册 ----
    async def _register_proxy(self, cfg: dict):
        """向服务端注册一个代理"""
        name = cfg["name"]
        proxy_type = cfg.get("type", PROXY_TCP)
        remote_port = cfg.get("remote_port", 0)

        if proxy_type == PROXY_TCP:
            self._proxy_clients[name] = TCPProxyClient(cfg, self)
        elif proxy_type == PROXY_UDP:
            client = UDPProxyClient(cfg, self)
            self._proxy_clients[name] = client
            await client.start()
        elif proxy_type == PROXY_XTCP:
            self._proxy_clients[name] = XTCPProxyClient(cfg, self)
        elif proxy_type in (PROXY_HTTP, PROXY_HTTPS):
            # HTTP/HTTPS 代理复用 TCPProxyClient（都是通过工作连接转发 TCP 流）
            self._proxy_clients[name] = TCPProxyClient(cfg, self)
        else:
            log.error(f"不支持的代理类型: {proxy_type}")
            return

        # 构建注册消息，包含 vhost 相关字段
        msg_data = {
            "name": name,
            "proxy_type": proxy_type,
            "remote_port": remote_port,
            "local_ip": cfg.get("local_ip", "127.0.0.1"),
            "local_port": cfg.get("local_port", 0),
            "secret_key": cfg.get("secret_key", ""),
        }
        # HTTP/HTTPS 代理需要额外字段
        if proxy_type in (PROXY_HTTP, PROXY_HTTPS):
            msg_data["custom_domains"] = cfg.get("custom_domains", [])
            msg_data["subdomain"] = cfg.get("subdomain", "")
            msg_data["http_user"] = cfg.get("http_user", "")
            msg_data["http_pwd"] = cfg.get("http_pwd", "")
            msg_data["host_header_rewrite"] = cfg.get("host_header_rewrite", "")

        await self.send_msg(MSG_NEW_PROXY, msg_data)
        log.info(f"[REG] 注册代理: {name} ({proxy_type})")

    async def _register_visitor(self, cfg: dict):
        """注册 XTCP 访问者"""
        name = cfg["name"]
        vis = XTCPVisitorClient(cfg, self)
        self._visitor_clients[name] = vis
        await vis.start()

    async def _handle_new_proxy_resp(self, data: dict):
        name = data.get("name", "")
        error = data.get("error", "")
        if error:
            log.error(f"[REG:{name}] 代理注册失败: {error}")
        else:
            log.info(f"[REG:{name}] 代理注册成功 -> {data.get('remote_addr', '')}")

    # ---- 工作连接 ----
    async def _handle_req_work_conn(self, _data: dict):
        """服务端请求建立新的工作连接"""
        if not self._alive:
            return
        task = asyncio.create_task(self._establish_work_conn())
        self._work_conn_tasks.append(task)

    async def _establish_work_conn(self):
        """建立一条工作连接到服务端"""
        try:
            reader, writer = await asyncio.open_connection(
                self.server_addr, self.server_port
            )
        except Exception as e:
            log.debug(f"建立工作连接失败: {e}")
            return

        await write_message(writer, MSG_NEW_WORK_CONN, {
            "run_id": self.run_id,
            "token": self.token,
        })

        # 读取 StartWorkConn 消息
        msg = await read_message(reader)
        if msg is None or msg.type != MSG_START_WORK_CONN:
            log.debug("工作连接: 未收到 StartWorkConn")
            writer.close()
            return

        proxy_name = msg.data.get("proxy_name", "")

        # 分发到对应的代理处理器
        pxy = self._proxy_clients.get(proxy_name)
        if pxy and isinstance(pxy, TCPProxyClient):
            await pxy.handle_work_conn(reader, writer)
        else:
            log.debug(f"工作连接: 未知代理 '{proxy_name}'")
            writer.close()

    # ---- UDP 数据包 (服务端→客户端) ----
    async def _handle_udp_packet(self, data: dict):
        """处理来自服务端的 UDP 数据包（来自公网用户）"""
        proxy_name = data.get("proxy_name", "")
        pxy = self._proxy_clients.get(proxy_name)
        if pxy and isinstance(pxy, UDPProxyClient):
            await pxy.handle_server_packet(data)

    # ---- NAT 穿透 ----
    async def _handle_nat_hole_client(self, data: dict):
        """服务端要求我们参与 NAT 穿透（我们是代理持有方）"""
        proxy_name = data.get("proxy_name", "")
        pxy = self._proxy_clients.get(proxy_name)
        if pxy and isinstance(pxy, XTCPProxyClient):
            await pxy.handle_nat_hole_client(data)

    async def _handle_nat_hole_resp(self, data: dict):
        """接收 NAT 穿透响应（访问者端）"""
        transaction_id = data.get("transaction_id", "")
        future = self._pending_resp.get(transaction_id)
        if future and not future.done():
            future.set_result(data)

    # ---- 心跳 ----
    async def _heartbeat_loop(self):
        """心跳循环：每10秒发送 ping"""
        while self._alive:
            await asyncio.sleep(10)
            if not self._alive:
                break
            try:
                await self.send_msg(MSG_PING, {"ts": now_ts()})
            except Exception:
                log.warning("心跳发送失败")
                self._alive = False
                break

    async def _handle_pong(self, _data: dict):
        pass  # 心跳响应正常

    # ---- 清理 ----
    async def _cleanup(self):
        """清理资源"""
        for name, pxy in self._proxy_clients.items():
            if hasattr(pxy, "close"):
                try:
                    await pxy.close()
                except Exception:
                    pass

        for name, vis in self._visitor_clients.items():
            try:
                await vis.close()
            except Exception:
                pass

        self._proxy_clients.clear()
        self._visitor_clients.clear()

        try:
            if self._writer:
                self._writer.close()
        except Exception:
            pass

    def disconnect(self):
        """断开连接"""
        self._alive = False
        for task in self._work_conn_tasks:
            if not task.done():
                task.cancel()
        self._work_conn_tasks.clear()
        if self._writer:
            try:
                self._writer.close()
            except Exception:
                pass


# ============================================================
#  辅助函数
# ============================================================
async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """单向数据管道"""
    try:
        while True:
            data = await reader.read(64 * 1024)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
        pass
    except Exception as e:
        log.debug(f"pipe error: {e}")
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _recvfrom_timeout(loop, sock: socket.socket, bufsize: int,
                             timeout: float) -> Tuple[bytes, Tuple]:
    """带超时的 UDP recvfrom"""
    future = loop.create_future()

    def _callback():
        if not future.done():
            try:
                data, addr = sock.recvfrom(bufsize)
                future.set_result((data, addr))
            except Exception as e:
                future.set_exception(e)

    loop.add_reader(sock, _callback)

    try:
        return await asyncio.wait_for(future, timeout=timeout)
    except asyncio.TimeoutError:
        raise
    finally:
        loop.remove_reader(sock)


# ============================================================
#  主入口
# ============================================================
def load_config(config_path: str) -> dict:
    """加载 JSON 配置文件"""
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def run_client(config: dict):
    """运行客户端主逻辑"""

    server_cfg = config.get("server", {})
    server_addr = server_cfg.get("addr", "127.0.0.1")
    server_port = server_cfg.get("port", 7000)
    token = server_cfg.get("token", "")
    pool_count = server_cfg.get("pool_count", 5)

    proxies = config.get("proxies", [])
    visitors = config.get("visitors", [])

    async def _run():
        session = ControlSession(server_addr, server_port, token, pool_count)
        while True:
            connected = await session.connect()
            if not connected:
                log.info(f"{server_addr}:{server_port} 连接失败，10秒后重试...")
                await asyncio.sleep(10)
                continue

            try:
                await session.run(proxies, visitors)
            except Exception as e:
                log.error(f"会话异常: {e}")

            if session._alive:
                log.info("会话断开，5秒后重连...")
                await asyncio.sleep(5)
            else:
                break

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _signal_handler():
        log.info("收到停止信号，正在关闭...")
        loop.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass

    try:
        loop.run_until_complete(_run())
    except KeyboardInterrupt:
        log.info("收到中断信号")
    finally:
        loop.close()


def generate_example_config(path: str):
    """生成示例配置文件"""
    example = {
        "server": {
            "addr": "your-server.com",
            "port": 7000,
            "token": "your-secret-token",
            "pool_count": 5,
        },
        "proxies": [
            {
                "name": "ssh",
                "type": "tcp",
                "local_ip": "127.0.0.1",
                "local_port": 22,
                "remote_port": 2222,
            },
            {
                "name": "web",
                "type": "tcp",
                "local_ip": "127.0.0.1",
                "local_port": 8080,
                "remote_port": 8080,
            },
            {
                "name": "dns",
                "type": "udp",
                "local_ip": "127.0.0.1",
                "local_port": 53,
                "remote_port": 5353,
            },
            {
                "name": "rdp_p2p",
                "type": "xtcp",
                "local_ip": "127.0.0.1",
                "local_port": 3389,
                "secret_key": "shared_secret_123",
            },
            {
                "name": "web_http",
                "type": "http",
                "local_ip": "127.0.0.1",
                "local_port": 8080,
                "custom_domains": ["www.example.com", "example.com"],
            },
            {
                "name": "web_https",
                "type": "https",
                "local_ip": "127.0.0.1",
                "local_port": 8080,
                "custom_domains": ["secure.example.com"],
            },
        ],
        "visitors": [
            {
                "name": "rdp_p2p_visitor",
                "type": "xtcp",
                "server_name": "rdp_p2p",
                "secret_key": "shared_secret_123",
                "bind_port": 13389,
                "local_ip": "0.0.0.0",
            },
        ],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(example, f, indent=2, ensure_ascii=False)
    log.info(f"示例配置文件已生成: {path}")


def main():
    parser = argparse.ArgumentParser(
        description="frp-lite 客户端 - 轻量级内网穿透",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python frpc_lite.py --config config.json
  python frpc_lite.py --generate-config   (生成示例配置)
        """,
    )
    parser.add_argument("--config", "-c", help="JSON 配置文件路径")
    parser.add_argument("--generate-config", action="store_true",
                        help="生成示例配置文件 frpc_config.json")

    args = parser.parse_args()

    if args.generate_config:
        generate_example_config("frpc_config.json")
        return

    if not args.config:
        parser.print_help()
        log.error("请指定配置文件: --config config.json")
        sys.exit(1)

    if not os.path.exists(args.config):
        log.error(f"配置文件不存在: {args.config}")
        sys.exit(1)

    config = load_config(args.config)
    run_client(config)


if __name__ == "__main__":
    main()