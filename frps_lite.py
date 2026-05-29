"""
frp-lite 服务端 (frps_lite)
============================
轻量级内网穿透服务端，运行在具有公网 IP 的服务器上。

功能:
    - 接收客户端控制连接，管理客户端会话
    - 工作连接池 (Work Connection Pool) 预分配管理
    - TCP 端口映射代理
    - UDP 数据包转发代理
    - XTCP NAT 穿透协调
    - Token 认证
    - 心跳检测

启动:
    python frps_lite.py --bind-addr 0.0.0.0 --bind-port 7000 --token mytoken123
"""

import asyncio
import argparse
import base64
import logging
import os
import signal
import socket
import struct
import sys
import time
from collections import deque
from typing import Dict, Optional, Set, Tuple

from frp_lite_protocol import (
    MSG_LOGIN, MSG_LOGIN_RESP, MSG_NEW_PROXY, MSG_NEW_PROXY_RESP,
    MSG_CLOSE_PROXY, MSG_REQ_WORK_CONN, MSG_NEW_WORK_CONN,
    MSG_START_WORK_CONN, MSG_PING, MSG_PONG, MSG_UDP_PACKET,
    MSG_NAT_HOLE_VISITOR, MSG_NAT_HOLE_CLIENT,
    MSG_NAT_HOLE_RESP, MSG_NAT_HOLE_SID,
    PROXY_TCP, PROXY_UDP, PROXY_XTCP,
    read_message, write_message, generate_run_id,
    generate_transaction_id, generate_sid,
    HEADER_SIZE, MAX_MSG_SIZE, pack_message,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("frps")


def now_ts() -> float:
    return time.time()


# ============================================================
#  TCP 代理 (端口映射)
# ============================================================
class TCPProxy:
    """服务端 TCP 代理：在公网端口监听，将用户连接通过工作连接转发到客户端"""

    def __init__(self, name: str, remote_port: int, control: "ControlHandler",
                 bind_addr: str = "0.0.0.0"):
        self.name = name
        self.remote_port = remote_port
        self.control = control
        self.bind_addr = bind_addr
        self.server: Optional[asyncio.AbstractServer] = None
        self._task: Optional[asyncio.Task] = None

    async def start(self):
        try:
            self.server = await asyncio.start_server(
                self._handle_user_conn, self.bind_addr, self.remote_port
            )
            log.info(f"[TCP:{self.name}] 监听端口 {self.remote_port}")
        except OSError as e:
            log.error(f"[TCP:{self.name}] 端口 {self.remote_port} 绑定失败: {e}")
            raise

    async def _handle_user_conn(self, reader: asyncio.StreamReader,
                                 writer: asyncio.StreamWriter):
        """处理外部用户连接"""
        peername = writer.get_extra_info("peername")
        sockname = writer.get_extra_info("sockname")
        log.info(f"[TCP:{self.name}] 收到用户连接 {peername}")

        try:
            work_reader, work_writer, _ = await self.control.get_work_conn()
        except Exception as e:
            log.warning(f"[TCP:{self.name}] 获取工作连接失败: {e}")
            writer.close()
            return

        try:
            src_addr, src_port = peername[0], peername[1]
            dst_addr, dst_port = sockname[0], sockname[1]

            start_data = {
                "proxy_name": self.name,
                "src_addr": src_addr,
                "src_port": src_port,
                "dst_addr": dst_addr,
                "dst_port": dst_port,
            }
            await write_message(work_writer, MSG_START_WORK_CONN, start_data)

            await asyncio.gather(
                _pipe(reader, work_writer),
                _pipe(work_reader, writer),
            )
        except Exception as e:
            log.debug(f"[TCP:{self.name}] 转发错误: {e}")
        finally:
            writer.close()
            try:
                work_writer.close()
            except Exception:
                pass

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            log.info(f"[TCP:{self.name}] 已关闭")


# ============================================================
#  UDP 代理
# ============================================================
class UDPProxy:
    """服务端 UDP 代理：在公网 UDP 端口监听转发数据包"""

    def __init__(self, name: str, remote_port: int, control: "ControlHandler",
                 bind_addr: str = "0.0.0.0"):
        self.name = name
        self.remote_port = remote_port
        self.control = control
        self.bind_addr = bind_addr
        self._sock: Optional[socket.socket] = None
        self._client_addr: Optional[Tuple] = None
        self._transport: Optional[asyncio.DatagramTransport] = None
        self._task: Optional[asyncio.Task] = None

    async def start(self):
        loop = asyncio.get_event_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _UDPProtocol(self),
            local_addr=(self.bind_addr, self.remote_port),
        )
        log.info(f"[UDP:{self.name}] 监听 UDP 端口 {self.remote_port}")

    async def close(self):
        if self._transport:
            self._transport.close()
            log.info(f"[UDP:{self.name}] 已关闭")


class _UDPProtocol(asyncio.DatagramProtocol):
    def __init__(self, proxy: UDPProxy):
        self.proxy = proxy

    def datagram_received(self, data: bytes, addr: Tuple):
        content_b64 = base64.b64encode(data).decode()
        packet_data = {
            "proxy_name": self.proxy.name,
            "content_b64": content_b64,
            "src_addr": addr[0],
            "src_port": addr[1],
            "local_addr": "",
            "local_port": self.proxy.remote_port,
        }
        asyncio.create_task(
            self.proxy.control.send_msg(MSG_UDP_PACKET, packet_data)
        )

    def error_received(self, exc):
        log.debug(f"[UDP:{self.proxy.name}] 错误: {exc}")

    def connection_lost(self, exc):
        pass


# ============================================================
#  XTCP 代理 (NAT 穿透协调)
# ============================================================
class XTCPProxy:
    """服务端 XTCP 代理：协调两个客户端进行 NAT 穿透"""

    def __init__(self, name: str, control: "ControlHandler"):
        self.name = name
        self.control = control
        self.pending_visitors: Dict[str, asyncio.Future] = {}

    async def handle_visitor(self, data: dict, visitor_control: "ControlHandler"):
        """处理访问者的 NAT 穿透请求"""
        transaction_id = data.get("transaction_id", "")
        pre_check = data.get("pre_check", False)

        if pre_check:
            resp = {
                "transaction_id": transaction_id,
                "proxy_name": self.name,
                "error": "",
                "sid": "",
                "client_mapped_addrs": [],
                "visitor_mapped_addrs": [],
            }
            await visitor_control.send_msg(MSG_NAT_HOLE_RESP, resp)
            return

        sid = generate_sid()
        visitor_mapped = visitor_control.peer_addr or "unknown"

        nat_client_data = {
            "transaction_id": transaction_id,
            "proxy_name": self.name,
            "sid": sid,
            "mapped_addrs": [visitor_mapped],
        }
        await self.control.send_msg(MSG_NAT_HOLE_CLIENT, nat_client_data)

        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self.pending_visitors[transaction_id] = future

        try:
            client_result = await asyncio.wait_for(future, timeout=15)
        except asyncio.TimeoutError:
            resp = {
                "transaction_id": transaction_id,
                "proxy_name": self.name,
                "error": "timeout waiting for client response",
                "sid": sid,
                "client_mapped_addrs": [],
                "visitor_mapped_addrs": [visitor_mapped],
            }
        else:
            resp = {
                "transaction_id": transaction_id,
                "proxy_name": self.name,
                "error": "",
                "sid": sid,
                "client_mapped_addrs": client_result.get("mapped_addrs", []),
                "visitor_mapped_addrs": [visitor_mapped],
            }

        await visitor_control.send_msg(MSG_NAT_HOLE_RESP, resp)
        self.pending_visitors.pop(transaction_id, None)


# ============================================================
#  控制连接处理器 (每个客户端一个 ControlHandler)
# ============================================================
class ControlHandler:
    """管理单个客户端的控制会话"""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                 token: str, server: "FrpServer"):
        self.reader = reader
        self.writer = writer
        self.token = token
        self.server = server
        self.run_id: str = ""
        self.pool_count: int = 5
        self.peer_addr: str = ""
        self._alive = True
        self._proxies: Dict[str, object] = {}
        # 工作连接池
        self._work_conn_pool: deque = deque()
        self._pool_event = asyncio.Event()
        self._send_lock = asyncio.Lock()

    async def send_msg(self, msg_type: str, data: dict = None):
        """线程安全地向客户端发送消息"""
        async with self._send_lock:
            await write_message(self.writer, msg_type, data)

    async def run(self):
        """主循环：处理控制消息"""
        self.peer_addr = self.writer.get_extra_info("peername")
        if self.peer_addr:
            self.peer_addr = f"{self.peer_addr[0]}:{self.peer_addr[1]}"
        log.info(f"[CTL] 新连接来自 {self.peer_addr}")

        try:
            while self._alive:
                msg = await read_message(self.reader)
                if msg is None:
                    break
                await self._dispatch(msg)
        except Exception as e:
            log.debug(f"[CTL:{self.run_id}] 连接错误: {e}")
        finally:
            await self._cleanup()

    async def _dispatch(self, msg):
        """消息分发"""
        handlers = {
            MSG_LOGIN: self._handle_login,
            MSG_NEW_PROXY: self._handle_new_proxy,
            MSG_CLOSE_PROXY: self._handle_close_proxy,
            MSG_PING: self._handle_ping,
            MSG_UDP_PACKET: self._handle_udp_packet,
            MSG_NAT_HOLE_VISITOR: self._handle_nat_hole_visitor,
            MSG_NAT_HOLE_RESP: self._handle_nat_hole_resp,
        }
        handler = handlers.get(msg.type)
        if handler:
            try:
                await handler(msg.data)
            except Exception as e:
                log.error(f"[CTL:{self.run_id}] 处理 {msg.type} 异常: {e}")
        else:
            log.debug(f"[CTL:{self.run_id}] 未知消息类型: {msg.type}")

    # ---- 登录处理 ----
    async def _handle_login(self, data: dict):
        client_token = data.get("token", "")
        if client_token != self.token:
            log.warning(f"[CTL] 登录失败: token 不匹配 ({self.peer_addr})")
            await self.send_msg(MSG_LOGIN_RESP, {"ok": False, "error": "invalid token"})
            self._alive = False
            return

        self.run_id = generate_run_id()
        self.pool_count = data.get("pool_count", 5)
        self.pool_count = max(1, min(self.pool_count, 50))

        await self.send_msg(MSG_LOGIN_RESP, {"ok": True, "run_id": self.run_id})
        log.info(f"[CTL:{self.run_id}] 客户端登录成功 (pool={self.pool_count})")

        # 预分配工作连接
        for _ in range(self.pool_count):
            await self.send_msg(MSG_REQ_WORK_CONN, {})

        # 启动心跳
        asyncio.create_task(self._heartbeat_checker())

    # ---- 代理注册 ----
    async def _handle_new_proxy(self, data: dict):
        name = data.get("name", "")
        proxy_type = data.get("proxy_type", PROXY_TCP)
        remote_port = data.get("remote_port", 0)

        if not name:
            await self.send_msg(MSG_NEW_PROXY_RESP, {"name": name, "error": "name required"})
            return

        if name in self._proxies:
            await self.send_msg(MSG_NEW_PROXY_RESP, {"name": name, "error": "already exists"})
            return

        try:
            if proxy_type == PROXY_TCP:
                pxy = TCPProxy(name, remote_port, self, self.server.bind_addr)
                await pxy.start()
            elif proxy_type == PROXY_UDP:
                pxy = UDPProxy(name, remote_port, self, self.server.bind_addr)
                await pxy.start()
            elif proxy_type == PROXY_XTCP:
                pxy = XTCPProxy(name, self)
            else:
                await self.send_msg(MSG_NEW_PROXY_RESP, {"name": name, "error": f"unsupported type: {proxy_type}"})
                return

            self._proxies[name] = pxy
            self.server._register_proxy(name, proxy_type, remote_port, self.run_id)

            remote_addr = f":{remote_port}" if remote_port else "(xtcp)"
            await self.send_msg(MSG_NEW_PROXY_RESP, {
                "name": name,
                "remote_addr": remote_addr,
                "error": "",
            })
            log.info(f"[{proxy_type.upper()}:{name}] 代理创建成功 (client={self.run_id})")

        except Exception as e:
            log.error(f"[{proxy_type.upper()}:{name}] 代理创建失败: {e}")
            await self.send_msg(MSG_NEW_PROXY_RESP, {"name": name, "error": str(e)})

    async def _handle_close_proxy(self, data: dict):
        name = data.get("name", "")
        pxy = self._proxies.pop(name, None)
        if pxy:
            self.server._unregister_proxy(name)
            if hasattr(pxy, "close"):
                await pxy.close()
            log.info(f"[PROXY:{name}] 代理已关闭")

    # ---- 工作连接池 ----
    def add_work_conn(self, reader: asyncio.StreamReader,
                         writer: asyncio.StreamWriter, data: dict):
        """添加工作连接到池中"""
        self._work_conn_pool.append((reader, writer, data))
        self._pool_event.set()

    async def get_work_conn(self, timeout: float = 10.0) -> Tuple:
        """从池中获取一个工作连接，如果池空则请求新连接"""
        if self._work_conn_pool:
            return self._work_conn_pool.popleft()
        if self._pool_event.is_set():
            self._pool_event.clear()
        try:
            await self.send_msg(MSG_REQ_WORK_CONN, {})
        except Exception:
            pass
        self._pool_event.clear()
        try:
            await asyncio.wait_for(self._pool_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise Exception("获取工作连接超时")

        if self._work_conn_pool:
            return self._work_conn_pool.popleft()
        raise Exception("无可用工作连接")

    # ---- 心跳 ----
    async def _handle_ping(self, data: dict):
        await self.send_msg(MSG_PONG, {"ts": now_ts()})

    async def _heartbeat_checker(self):
        """检查心跳超时（30秒无心跳则断开）"""
        last_ping = now_ts()
        timeout = 30.0

        async def _update():
            nonlocal last_ping
            last_ping = now_ts()

        # 覆盖 ping 处理来更新时间
        orig_ping = self._handle_ping

        async def _ping_with_timer(data):
            nonlocal last_ping
            last_ping = now_ts()
            await orig_ping(data)

        self._handle_ping = _ping_with_timer

        while self._alive:
            await asyncio.sleep(5)
            if now_ts() - last_ping > timeout:
                log.warning(f"[CTL:{self.run_id}] 心跳超时，断开连接")
                self._alive = False
                self.writer.close()
                break

    # ---- UDP 数据包转发 (服务端→客户端) ----
    async def _handle_udp_packet(self, data: dict):
        """将来自用户的 UDP 包转发给客户端"""
        # 通过控制连接直接转发UDP数据包
        # 客户端收到后转发给本地服务
        pass  # UDP包由客户端→服务端方向在 _UDPProtocol 中处理，服务端→客户端方向由客户端主动发送

    # ---- NAT 穿透处理 ----
    async def _handle_nat_hole_visitor(self, data: dict):
        """访问者发起的 NAT 穿透请求"""
        proxy_name = data.get("proxy_name", "")
        pxy = self._proxies.get(proxy_name)
        if pxy and isinstance(pxy, XTCPProxy):
            await pxy.handle_visitor(data, self)
        else:
            await self.send_msg(MSG_NAT_HOLE_RESP, {
                "transaction_id": data.get("transaction_id", ""),
                "proxy_name": proxy_name,
                "error": f"xtcp proxy '{proxy_name}' not found",
            })

    async def _handle_nat_hole_resp(self, data: dict):
        """客户端对 NAT 穿透请求的响应"""
        transaction_id = data.get("transaction_id", "")
        proxy_name = data.get("proxy_name", "")
        pxy = self._proxies.get(proxy_name)
        if pxy and isinstance(pxy, XTCPProxy):
            future = pxy.pending_visitors.get(transaction_id)
            if future and not future.done():
                future.set_result(data)

    # ---- 清理 ----
    async def _cleanup(self):
        self._alive = False
        for name, pxy in list(self._proxies.items()):
            self.server._unregister_proxy(name)
            if hasattr(pxy, "close"):
                try:
                    await pxy.close()
                except Exception:
                    pass
        self._proxies.clear()
        self.server._remove_control(self)
        try:
            self.writer.close()
        except Exception:
            pass
        log.info(f"[CTL:{self.run_id}] 会话已关闭")


# ============================================================
#  主服务器
# ============================================================
class FrpServer:
    """frp-lite 服务端主控"""

    def __init__(self, bind_addr: str, bind_port: int, token: str,
                 proxy_bind_addr: str = "0.0.0.0"):
        self.bind_addr = bind_addr
        self.bind_port = bind_port
        self.proxy_bind_addr = proxy_bind_addr
        self.token = token
        self._server: Optional[asyncio.AbstractServer] = None
        self._controls: Dict[str, ControlHandler] = {}
        self._proxies: Dict[str, dict] = {}
        self._running = False

    async def start(self):
        """启动服务"""
        self._loop = asyncio.get_running_loop()
        self._server = await asyncio.start_server(
            self._handle_conn, self.bind_addr, self.bind_port
        )
        self._running = True
        log.info(f"frps-lite 启动成功 -> {self.bind_addr}:{self.bind_port}")
        log.info(f"Token: {self.token}")

        addrs = []
        for s in self._server.sockets:
            addr = s.getsockname()
            addrs.append(f"{addr[0]}:{addr[1]}")
        log.info(f"监听地址: {', '.join(addrs)}")

        asyncio.ensure_future(self._server.serve_forever())

    async def _handle_conn(self, reader: asyncio.StreamReader,
                            writer: asyncio.StreamWriter):
        """处理新连接：区分控制连接和工作连接"""
        first_msg = await read_message(reader)
        if first_msg is None:
            writer.close()
            return

        if first_msg.type == MSG_NEW_WORK_CONN:
            run_id = first_msg.data.get("run_id", "")
            ctl = self._find_control(run_id)
            if ctl is None:
                log.warning(f"工作连接: 未找到控制会话 run_id={run_id}")
                writer.close()
                return
            ctl.add_work_conn(reader, writer, first_msg.data)
            return

        if first_msg.type == MSG_LOGIN:
            ctl = ControlHandler(reader, writer, self.token, self)
            self._controls[ctl.run_id or str(id(ctl))] = ctl
            try:
                await ctl._dispatch(first_msg)
                await ctl.run()
            except Exception as e:
                log.debug(f"控制会话异常: {e}")
        else:
            log.debug(f"未知首消息类型: {first_msg.type}")
            writer.close()

    def _find_control(self, run_id: str) -> Optional[ControlHandler]:
        """根据 run_id 查找控制会话"""
        for ctl in self._controls.values():
            if ctl.run_id == run_id:
                return ctl
        return None

    def _register_proxy(self, name: str, proxy_type: str,
                         remote_port: int, run_id: str):
        self._proxies[name] = {
            "type": proxy_type,
            "remote_port": remote_port,
            "run_id": run_id,
        }

    def _unregister_proxy(self, name: str):
        self._proxies.pop(name, None)

    def _remove_control(self, ctl: ControlHandler):
        for key, val in list(self._controls.items()):
            if val is ctl:
                del self._controls[key]
                break

    async def stop(self):
        """停止服务"""
        self._running = False
        for ctl in list(self._controls.values()):
            try:
                ctl.writer.close()
            except Exception:
                pass
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        log.info("frps-lite 已停止")


# ============================================================
#  数据管道辅助函数
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


# ============================================================
#  主入口
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="frp-lite 服务端 - 轻量级内网穿透",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python frps_lite.py --bind-port 7000 --token mysecret
  python frps_lite.py --bind-addr 0.0.0.0 --bind-port 7000 --token mysecret --proxy-bind-addr 0.0.0.0
        """,
    )
    parser.add_argument("--bind-addr", default="0.0.0.0", help="控制连接监听地址 (默认: 0.0.0.0)")
    parser.add_argument("--bind-port", type=int, default=7000, help="控制连接监听端口 (默认: 7000)")
    parser.add_argument("--token", default=None, help="认证 token (不指定则自动生成)")
    parser.add_argument("--proxy-bind-addr", default="0.0.0.0", help="代理端口绑定地址 (默认: 0.0.0.0)")

    args = parser.parse_args()

    token = args.token or generate_sid()
    if not args.token:
        log.info(f"未指定 token，已自动生成: {token}")

    server = FrpServer(
        bind_addr=args.bind_addr,
        bind_port=args.bind_port,
        token=token,
        proxy_bind_addr=args.proxy_bind_addr,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _signal_handler():
        log.info("收到停止信号，正在关闭...")
        asyncio.ensure_future(server.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass

    try:
        loop.run_until_complete(server.start())
    except KeyboardInterrupt:
        log.info("收到中断信号")
    finally:
        loop.run_until_complete(server.stop())
        loop.close()


if __name__ == "__main__":
    main()