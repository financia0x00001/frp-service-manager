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
    PROXY_TCP, PROXY_UDP, PROXY_XTCP, PROXY_HTTP, PROXY_HTTPS,
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
#  HTTP vhost 代理 (基于域名的 HTTP 虚拟主机路由)
# ============================================================
class HTTPVhostProxy:
    """服务端 HTTP vhost 代理：根据 Host 头路由到对应客户端

    多个 HTTP 代理共享同一个 vhost 端口（默认80），
    通过请求头中的 Host 字段区分不同的代理。
    """

    def __init__(self, name: str, custom_domains: list, control: "ControlHandler",
                 subdomain: str = "", http_user: str = "", http_pwd: str = "",
                 host_header_rewrite: str = ""):
        self.name = name
        self.custom_domains = [d.strip().lower() for d in custom_domains if d.strip()]
        self.subdomain = subdomain.strip().lower()
        self.control = control
        self.http_user = http_user
        self.http_pwd = http_pwd
        self.host_header_rewrite = host_header_rewrite
        # 不单独监听端口，由 VhostServer 统一监听

    def match_host(self, host: str) -> bool:
        """检查请求的 Host 是否匹配此代理"""
        if not host:
            return False
        host = host.lower()
        # 去掉端口部分
        if ":" in host:
            host = host.split(":")[0]
        if host in self.custom_domains:
            return True
        if self.subdomain and self.control.server.subdomain_host:
            full_domain = f"{self.subdomain}.{self.control.server.subdomain_host}"
            if host == full_domain:
                return True
        return False

    async def handle_request(self, reader: asyncio.StreamReader,
                              writer: asyncio.StreamWriter, host: str):
        """处理匹配到的 HTTP 请求"""
        # HTTP Basic Auth 校验
        if self.http_user and self.http_pwd:
            if not self._check_http_auth(reader, writer):
                return

        try:
            work_reader, work_writer, _ = await self.control.get_work_conn()
        except Exception as e:
            log.warning(f"[HTTP:{self.name}] 获取工作连接失败: {e}")
            writer.close()
            return

        try:
            # 读取已消费的 HTTP 请求头数据，需要重新发送给客户端
            # 由于我们已经在 VhostHTTPServer 中读取了首行和头部，
            # 这里需要将完整的 HTTP 请求重新构造发送给工作连接
            start_data = {
                "proxy_name": self.name,
                "src_addr": writer.get_extra_info("peername")[0] if writer.get_extra_info("peername") else "",
                "src_port": writer.get_extra_info("peername")[1] if writer.get_extra_info("peername") else 0,
                "dst_addr": "",
                "dst_port": 0,
            }
            await write_message(work_writer, MSG_START_WORK_CONN, start_data)

            # 将缓存的原始请求数据发送到工作连接
            # VhostHTTPServer 会把原始请求字节缓存到 reader 对象上
            raw_data = getattr(reader, '_vhost_raw_data', b'')
            if raw_data:
                work_writer.write(raw_data)
                await work_writer.drain()

            await asyncio.gather(
                _pipe(reader, work_writer),
                _pipe(work_reader, writer),
            )
        except Exception as e:
            log.debug(f"[HTTP:{self.name}] 转发错误: {e}")
        finally:
            writer.close()
            try:
                work_writer.close()
            except Exception:
                pass

    def _check_http_auth(self, reader, writer) -> bool:
        """检查 HTTP Basic 认证（简化实现）"""
        # 认证在 VhostHTTPServer 中处理
        return True

    async def close(self):
        log.info(f"[HTTP:{self.name}] 已关闭")


# ============================================================
#  HTTPS vhost 代理 (TLS 终止 + 基于域名的路由)
# ============================================================
class HTTPSVhostProxy:
    """服务端 HTTPS vhost 代理：TLS 终止后根据 Host 头路由

    服务端持有 SSL 证书，在 vhost HTTPS 端口（默认443）上监听，
    TLS 握手后读取 HTTP 请求的 Host 头进行路由。
    """

    def __init__(self, name: str, custom_domains: list, control: "ControlHandler",
                 subdomain: str = "", http_user: str = "", http_pwd: str = "",
                 host_header_rewrite: str = ""):
        self.name = name
        self.custom_domains = [d.strip().lower() for d in custom_domains if d.strip()]
        self.subdomain = subdomain.strip().lower()
        self.control = control
        self.http_user = http_user
        self.http_pwd = http_pwd
        self.host_header_rewrite = host_header_rewrite

    def match_host(self, host: str) -> bool:
        """检查请求的 Host 是否匹配此代理"""
        if not host:
            return False
        host = host.lower()
        if ":" in host:
            host = host.split(":")[0]
        if host in self.custom_domains:
            return True
        if self.subdomain and self.control.server.subdomain_host:
            full_domain = f"{self.subdomain}.{self.control.server.subdomain_host}"
            if host == full_domain:
                return True
        return False

    async def handle_request(self, reader: asyncio.StreamReader,
                              writer: asyncio.StreamWriter, host: str):
        """处理匹配到的 HTTPS 请求（TLS 已在 VhostHTTPSServer 中终止）"""
        try:
            work_reader, work_writer, _ = await self.control.get_work_conn()
        except Exception as e:
            log.warning(f"[HTTPS:{self.name}] 获取工作连接失败: {e}")
            writer.close()
            return

        try:
            start_data = {
                "proxy_name": self.name,
                "src_addr": writer.get_extra_info("peername")[0] if writer.get_extra_info("peername") else "",
                "src_port": writer.get_extra_info("peername")[1] if writer.get_extra_info("peername") else 0,
                "dst_addr": "",
                "dst_port": 0,
            }
            await write_message(work_writer, MSG_START_WORK_CONN, start_data)

            raw_data = getattr(reader, '_vhost_raw_data', b'')
            if raw_data:
                work_writer.write(raw_data)
                await work_writer.drain()

            await asyncio.gather(
                _pipe(reader, work_writer),
                _pipe(work_reader, writer),
            )
        except Exception as e:
            log.debug(f"[HTTPS:{self.name}] 转发错误: {e}")
        finally:
            writer.close()
            try:
                work_writer.close()
            except Exception:
                pass

    async def close(self):
        log.info(f"[HTTPS:{self.name}] 已关闭")


# ============================================================
#  Vhost HTTP 服务器 (共享80端口，按Host路由)
# ============================================================
class VhostHTTPServer:
    """HTTP vhost 服务器：在指定端口监听，根据 Host 头路由到不同代理"""

    def __init__(self, bind_addr: str, vhost_http_port: int, server: "FrpServer"):
        self.bind_addr = bind_addr
        self.vhost_http_port = vhost_http_port
        self.server = server
        self._tcp_server: Optional[asyncio.AbstractServer] = None

    async def start(self):
        try:
            self._tcp_server = await asyncio.start_server(
                self._handle_conn, self.bind_addr, self.vhost_http_port
            )
            log.info(f"[VhostHTTP] 监听端口 {self.bind_addr}:{self.vhost_http_port}")
        except OSError as e:
            log.error(f"[VhostHTTP] 端口 {self.vhost_http_port} 绑定失败: {e}")
            raise

    async def _handle_conn(self, reader: asyncio.StreamReader,
                            writer: asyncio.StreamWriter):
        """处理 HTTP 连接：读取 Host 头，路由到对应代理"""
        peername = writer.get_extra_info("peername")
        raw_data = b""

        try:
            # 读取 HTTP 请求行
            request_line = await asyncio.wait_for(reader.readline(), timeout=30.0)
            raw_data += request_line

            # 读取请求头
            headers_data = b""
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=30.0)
                headers_data += line
                if line == b"\r\n" or line == b"\n" or not line:
                    break
            raw_data += headers_data

            # 解析 Host 头
            host = self._parse_host(headers_data)
            if not host:
                self._send_404(writer)
                return

            log.info(f"[VhostHTTP] 请求 Host={host} from {peername}")

            # 查找匹配的代理
            proxy = self.server._find_vhost_proxy(PROXY_HTTP, host)
            if not proxy:
                self._send_404(writer, host)
                return

            # HTTP Basic Auth 校验
            if proxy.http_user and proxy.http_pwd:
                auth_header = self._parse_header(headers_data, "Authorization")
                if not self._verify_basic_auth(auth_header, proxy.http_user, proxy.http_pwd):
                    self._send_401(writer)
                    return

            # 将原始请求数据缓存到 reader 上，供代理转发使用
            reader._vhost_raw_data = raw_data
            await proxy.handle_request(reader, writer, host)

        except asyncio.TimeoutError:
            log.debug(f"[VhostHTTP] 读取请求超时 from {peername}")
            writer.close()
        except Exception as e:
            log.debug(f"[VhostHTTP] 处理连接错误: {e}")
            writer.close()

    def _parse_host(self, headers_data: bytes) -> str:
        """从 HTTP 请求头中解析 Host"""
        try:
            headers_str = headers_data.decode("utf-8", errors="ignore")
            for line in headers_str.split("\r\n"):
                if line.lower().startswith("host:"):
                    host = line.split(":", 1)[1].strip()
                    return host
        except Exception:
            pass
        return ""

    def _parse_header(self, headers_data: bytes, header_name: str) -> str:
        """从 HTTP 请求头中解析指定头部"""
        try:
            headers_str = headers_data.decode("utf-8", errors="ignore")
            for line in headers_str.split("\r\n"):
                if line.lower().startswith(header_name.lower() + ":"):
                    return line.split(":", 1)[1].strip()
        except Exception:
            pass
        return ""

    def _verify_basic_auth(self, auth_header: str, user: str, pwd: str) -> bool:
        """验证 HTTP Basic Auth"""
        import base64 as _b64
        if not auth_header or not auth_header.startswith("Basic "):
            return False
        try:
            decoded = _b64.b64decode(auth_header[6:]).decode("utf-8")
            parts = decoded.split(":", 1)
            if len(parts) == 2 and parts[0] == user and parts[1] == pwd:
                return True
        except Exception:
            pass
        return False

    def _send_404(self, writer: asyncio.StreamWriter, host: str = ""):
        body = f"404 Not Found - No proxy found for host: {host}".encode()
        resp = (
            b"HTTP/1.1 404 Not Found\r\n"
            b"Content-Type: text/plain\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Connection: close\r\n\r\n" + body
        )
        writer.write(resp)
        try:
            writer.close()
        except Exception:
            pass

    def _send_401(self, writer: asyncio.StreamWriter):
        body = b"401 Unauthorized"
        resp = (
            b"HTTP/1.1 401 Unauthorized\r\n"
            b"WWW-Authenticate: Basic realm=\"frp\"\r\n"
            b"Content-Type: text/plain\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Connection: close\r\n\r\n" + body
        )
        writer.write(resp)
        try:
            writer.close()
        except Exception:
            pass

    async def close(self):
        if self._tcp_server:
            self._tcp_server.close()
            await self._tcp_server.wait_closed()
            log.info("[VhostHTTP] 已关闭")


# ============================================================
#  Vhost HTTPS 服务器 (共享443端口，TLS终止 + 按Host路由)
# ============================================================
class VhostHTTPSServer:
    """HTTPS vhost 服务器：TLS 终止后根据 Host 头路由到不同代理"""

    def __init__(self, bind_addr: str, vhost_https_port: int, server: "FrpServer",
                 cert_file: str = "", key_file: str = ""):
        self.bind_addr = bind_addr
        self.vhost_https_port = vhost_https_port
        self.server = server
        self.cert_file = cert_file
        self.key_file = key_file
        self._tcp_server: Optional[asyncio.AbstractServer] = None

    async def start(self):
        if not self.cert_file or not self.key_file:
            log.warning("[VhostHTTPS] 未配置证书，HTTPS vhost 未启动")
            return
        if not os.path.exists(self.cert_file):
            log.error(f"[VhostHTTPS] 证书文件不存在: {self.cert_file}")
            return
        if not os.path.exists(self.key_file):
            log.error(f"[VhostHTTPS] 私钥文件不存在: {self.key_file}")
            return

        try:
            import ssl
            ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_ctx.load_cert_chain(self.cert_file, self.key_file)

            self._tcp_server = await asyncio.start_server(
                self._handle_conn, self.bind_addr, self.vhost_https_port,
                ssl=ssl_ctx
            )
            log.info(f"[VhostHTTPS] 监听端口 {self.bind_addr}:{self.vhost_https_port} (cert={self.cert_file})")
        except OSError as e:
            log.error(f"[VhostHTTPS] 端口 {self.vhost_https_port} 绑定失败: {e}")
            raise

    async def _handle_conn(self, reader: asyncio.StreamReader,
                            writer: asyncio.StreamWriter):
        """处理 HTTPS 连接：TLS 已终止，读取 Host 头路由"""
        peername = writer.get_extra_info("peername")
        raw_data = b""

        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=30.0)
            raw_data += request_line

            headers_data = b""
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=30.0)
                headers_data += line
                if line == b"\r\n" or line == b"\n" or not line:
                    break
            raw_data += headers_data

            host = self._parse_host(headers_data)
            if not host:
                self._send_404(writer)
                return

            log.info(f"[VhostHTTPS] 请求 Host={host} from {peername}")

            proxy = self.server._find_vhost_proxy(PROXY_HTTPS, host)
            if not proxy:
                # 也尝试匹配 HTTP 代理（HTTP/HTTPS 共享域名配置）
                proxy = self.server._find_vhost_proxy(PROXY_HTTP, host)
            if not proxy:
                self._send_404(writer, host)
                return

            # HTTP Basic Auth
            if proxy.http_user and proxy.http_pwd:
                auth_header = self._parse_header(headers_data, "Authorization")
                if not self._verify_basic_auth(auth_header, proxy.http_user, proxy.http_pwd):
                    self._send_401(writer)
                    return

            reader._vhost_raw_data = raw_data
            await proxy.handle_request(reader, writer, host)

        except asyncio.TimeoutError:
            log.debug(f"[VhostHTTPS] 读取请求超时 from {peername}")
            writer.close()
        except Exception as e:
            log.debug(f"[VhostHTTPS] 处理连接错误: {e}")
            writer.close()

    def _parse_host(self, headers_data: bytes) -> str:
        try:
            headers_str = headers_data.decode("utf-8", errors="ignore")
            for line in headers_str.split("\r\n"):
                if line.lower().startswith("host:"):
                    return line.split(":", 1)[1].strip()
        except Exception:
            pass
        return ""

    def _parse_header(self, headers_data: bytes, header_name: str) -> str:
        try:
            headers_str = headers_data.decode("utf-8", errors="ignore")
            for line in headers_str.split("\r\n"):
                if line.lower().startswith(header_name.lower() + ":"):
                    return line.split(":", 1)[1].strip()
        except Exception:
            pass
        return ""

    def _verify_basic_auth(self, auth_header: str, user: str, pwd: str) -> bool:
        import base64 as _b64
        if not auth_header or not auth_header.startswith("Basic "):
            return False
        try:
            decoded = _b64.b64decode(auth_header[6:]).decode("utf-8")
            parts = decoded.split(":", 1)
            if len(parts) == 2 and parts[0] == user and parts[1] == pwd:
                return True
        except Exception:
            pass
        return False

    def _send_404(self, writer: asyncio.StreamWriter, host: str = ""):
        body = f"404 Not Found - No proxy found for host: {host}".encode()
        resp = (
            b"HTTP/1.1 404 Not Found\r\n"
            b"Content-Type: text/plain\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Connection: close\r\n\r\n" + body
        )
        writer.write(resp)
        try:
            writer.close()
        except Exception:
            pass

    def _send_401(self, writer: asyncio.StreamWriter):
        body = b"401 Unauthorized"
        resp = (
            b"HTTP/1.1 401 Unauthorized\r\n"
            b"WWW-Authenticate: Basic realm=\"frp\"\r\n"
            b"Content-Type: text/plain\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Connection: close\r\n\r\n" + body
        )
        writer.write(resp)
        try:
            writer.close()
        except Exception:
            pass

    async def close(self):
        if self._tcp_server:
            self._tcp_server.close()
            await self._tcp_server.wait_closed()
            log.info("[VhostHTTPS] 已关闭")


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
        custom_domains = data.get("custom_domains", [])
        subdomain = data.get("subdomain", "")
        http_user = data.get("http_user", "")
        http_pwd = data.get("http_pwd", "")
        host_header_rewrite = data.get("host_header_rewrite", "")

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
            elif proxy_type == PROXY_HTTP:
                if not custom_domains and not subdomain:
                    await self.send_msg(MSG_NEW_PROXY_RESP, {
                        "name": name, "error": "http proxy requires custom_domains or subdomain"
                    })
                    return
                pxy = HTTPVhostProxy(name, custom_domains, self,
                                      subdomain=subdomain,
                                      http_user=http_user, http_pwd=http_pwd,
                                      host_header_rewrite=host_header_rewrite)
                # 确保 vhost HTTP 服务器已启动
                self.server._ensure_vhost_http()
            elif proxy_type == PROXY_HTTPS:
                if not custom_domains and not subdomain:
                    await self.send_msg(MSG_NEW_PROXY_RESP, {
                        "name": name, "error": "https proxy requires custom_domains or subdomain"
                    })
                    return
                pxy = HTTPSVhostProxy(name, custom_domains, self,
                                       subdomain=subdomain,
                                       http_user=http_user, http_pwd=http_pwd,
                                       host_header_rewrite=host_header_rewrite)
                # 确保 vhost HTTPS 服务器已启动
                self.server._ensure_vhost_https()
            else:
                await self.send_msg(MSG_NEW_PROXY_RESP, {"name": name, "error": f"unsupported type: {proxy_type}"})
                return

            self._proxies[name] = pxy
            self.server._register_proxy(name, proxy_type, remote_port, self.run_id,
                                         custom_domains=custom_domains, subdomain=subdomain)

            if proxy_type in (PROXY_HTTP, PROXY_HTTPS):
                remote_addr = f"vhost({', '.join(custom_domains)})"
            elif proxy_type == PROXY_XTCP:
                remote_addr = "(xtcp)"
            else:
                remote_addr = f":{remote_port}"
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
                 proxy_bind_addr: str = "0.0.0.0",
                 vhost_http_port: int = 0, vhost_https_port: int = 0,
                 cert_file: str = "", key_file: str = "",
                 subdomain_host: str = ""):
        self.bind_addr = bind_addr
        self.bind_port = bind_port
        self.proxy_bind_addr = proxy_bind_addr
        self.token = token
        self.vhost_http_port = vhost_http_port
        self.vhost_https_port = vhost_https_port
        self.cert_file = cert_file
        self.key_file = key_file
        self.subdomain_host = subdomain_host
        self._server: Optional[asyncio.AbstractServer] = None
        self._controls: Dict[str, ControlHandler] = {}
        self._proxies: Dict[str, dict] = {}
        self._running = False
        # vhost 服务器实例
        self._vhost_http_server: Optional[VhostHTTPServer] = None
        self._vhost_https_server: Optional[VhostHTTPSServer] = None

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

        # 启动 vhost HTTP 服务器
        if self.vhost_http_port > 0:
            self._ensure_vhost_http()

        # 启动 vhost HTTPS 服务器
        if self.vhost_https_port > 0:
            self._ensure_vhost_https()

        if self.subdomain_host:
            log.info(f"子域名根: {self.subdomain_host}")

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

    def _register_proxy(self, name: str, proxy_type: str, remote_port: int,
                         run_id: str, custom_domains: list = None, subdomain: str = ""):
        self._proxies[name] = {
            "type": proxy_type,
            "remote_port": remote_port,
            "run_id": run_id,
            "custom_domains": custom_domains or [],
            "subdomain": subdomain,
        }

    def _find_vhost_proxy(self, proxy_type: str, host: str):
        """查找匹配指定 Host 的 vhost 代理"""
        if not host:
            return None
        host = host.lower()
        if ":" in host:
            host = host.split(":")[0]

        for ctl in self._controls.values():
            for name, pxy in ctl._proxies.items():
                if isinstance(pxy, (HTTPVhostProxy, HTTPSVhostProxy)):
                    if proxy_type == PROXY_HTTP and isinstance(pxy, HTTPVhostProxy):
                        if pxy.match_host(host):
                            return pxy
                    elif proxy_type == PROXY_HTTPS and isinstance(pxy, HTTPSVhostProxy):
                        if pxy.match_host(host):
                            return pxy
        return None

    def _ensure_vhost_http(self):
        """确保 vhost HTTP 服务器已启动"""
        if self._vhost_http_server is None and self.vhost_http_port > 0:
            self._vhost_http_server = VhostHTTPServer(
                self.proxy_bind_addr, self.vhost_http_port, self
            )
            asyncio.ensure_future(self._vhost_http_server.start())

    def _ensure_vhost_https(self):
        """确保 vhost HTTPS 服务器已启动"""
        if self._vhost_https_server is None and self.vhost_https_port > 0:
            self._vhost_https_server = VhostHTTPSServer(
                self.proxy_bind_addr, self.vhost_https_port, self,
                cert_file=self.cert_file, key_file=self.key_file
            )
            asyncio.ensure_future(self._vhost_https_server.start())

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
        if self._vhost_http_server:
            await self._vhost_http_server.close()
            self._vhost_http_server = None
        if self._vhost_https_server:
            await self._vhost_https_server.close()
            self._vhost_https_server = None
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
    parser.add_argument("--vhost-http-port", type=int, default=0, help="HTTP vhost 监听端口 (默认: 0 不启用)")
    parser.add_argument("--vhost-https-port", type=int, default=0, help="HTTPS vhost 监听端口 (默认: 0 不启用)")
    parser.add_argument("--cert-file", default="", help="HTTPS 证书文件路径 (PEM 格式)")
    parser.add_argument("--key-file", default="", help="HTTPS 私钥文件路径 (PEM 格式)")
    parser.add_argument("--subdomain-host", default="", help="子域名根 (如: frp.example.com)")

    args = parser.parse_args()

    token = args.token or generate_sid()
    if not args.token:
        log.info(f"未指定 token，已自动生成: {token}")

    server = FrpServer(
        bind_addr=args.bind_addr,
        bind_port=args.bind_port,
        token=token,
        proxy_bind_addr=args.proxy_bind_addr,
        vhost_http_port=args.vhost_http_port,
        vhost_https_port=args.vhost_https_port,
        cert_file=args.cert_file,
        key_file=args.key_file,
        subdomain_host=args.subdomain_host,
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