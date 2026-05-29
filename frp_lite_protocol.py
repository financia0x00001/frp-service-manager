"""
frp-lite 共享协议层
=====================
基于 JSON + 4字节长度前缀的轻量级消息协议。

消息格式:
    [4字节 big-endian 消息长度][JSON UTF-8 字节]

消息类型一览:
    login / login_resp           - 客户端登录认证
    new_proxy / new_proxy_resp   - 注册/响应代理
    close_proxy                  - 关闭代理
    req_work_conn                - 服务端请求新的工作连接
    new_work_conn                - 客户端建立工作连接
    start_work_conn              - 在工作连接上告知代理名称
    ping / pong                  - 心跳
    udp_packet                   - UDP 数据包转发
    nat_hole_visitor             - 访问者请求 NAT 穿透
    nat_hole_client              - NAT 穿透客户端行为
    nat_hole_resp                - NAT 穿透响应
    nat_hole_sid                 - NAT 穿透会话ID通知
"""

import struct
import json
import asyncio
import time
import hashlib
import secrets
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, List, Any


# ============ 消息类型常量 ============
MSG_LOGIN = "login"
MSG_LOGIN_RESP = "login_resp"
MSG_NEW_PROXY = "new_proxy"
MSG_NEW_PROXY_RESP = "new_proxy_resp"
MSG_CLOSE_PROXY = "close_proxy"
MSG_REQ_WORK_CONN = "req_work_conn"
MSG_NEW_WORK_CONN = "new_work_conn"
MSG_START_WORK_CONN = "start_work_conn"
MSG_PING = "ping"
MSG_PONG = "pong"
MSG_UDP_PACKET = "udp_packet"
MSG_NAT_HOLE_VISITOR = "nat_hole_visitor"
MSG_NAT_HOLE_CLIENT = "nat_hole_client"
MSG_NAT_HOLE_RESP = "nat_hole_resp"
MSG_NAT_HOLE_SID = "nat_hole_sid"

# 代理类型
PROXY_TCP = "tcp"
PROXY_UDP = "udp"
PROXY_HTTP = "http"
PROXY_HTTPS = "https"
PROXY_XTCP = "xtcp"


# ============ 消息结构 ============
@dataclass
class Message:
    type: str
    data: dict = field(default_factory=dict)


# ============ 具体消息体定义 ============
@dataclass
class LoginData:
    version: str = "1.0"
    hostname: str = ""
    token: str = ""
    pool_count: int = 5
    client_id: str = ""
    metas: Dict[str, str] = field(default_factory=dict)


@dataclass
class LoginRespData:
    ok: bool = False
    run_id: str = ""
    error: str = ""


@dataclass
class NewProxyData:
    name: str = ""
    proxy_type: str = PROXY_TCP
    remote_port: int = 0
    local_ip: str = "127.0.0.1"
    local_port: int = 0
    use_encryption: bool = False
    custom_domains: List[str] = field(default_factory=list)
    subdomain: str = ""
    locations: List[str] = field(default_factory=list)
    http_user: str = ""
    http_pwd: str = ""
    host_header_rewrite: str = ""
    # XTCP / STCP
    secret_key: str = ""
    allow_users: List[str] = field(default_factory=list)


@dataclass
class NewProxyRespData:
    name: str = ""
    remote_addr: str = ""
    error: str = ""


@dataclass
class StartWorkConnData:
    proxy_name: str = ""
    src_addr: str = ""
    src_port: int = 0
    dst_addr: str = ""
    dst_port: int = 0


@dataclass
class UDPPacketData:
    proxy_name: str = ""
    content_b64: str = ""
    src_addr: str = ""
    src_port: int = 0
    local_addr: str = ""
    local_port: int = 0


@dataclass
class NatHoleVisitorData:
    transaction_id: str = ""
    proxy_name: str = ""
    pre_check: bool = False
    protocol: str = "udp"
    mapped_addrs: List[str] = field(default_factory=list)


@dataclass
class NatHoleClientData:
    transaction_id: str = ""
    proxy_name: str = ""
    sid: str = ""
    mapped_addrs: List[str] = field(default_factory=list)


@dataclass
class NatHoleRespData:
    transaction_id: str = ""
    proxy_name: str = ""
    error: str = ""
    sid: str = ""
    client_mapped_addrs: List[str] = field(default_factory=list)
    visitor_mapped_addrs: List[str] = field(default_factory=list)
    detect_mode: int = 0


@dataclass
class NatHoleSidData:
    transaction_id: str = ""
    proxy_name: str = ""
    sid: str = ""
    ok: bool = False


# ============ 协议读写辅助函数 ============
HEADER_SIZE = 4
MAX_MSG_SIZE = 16 * 1024 * 1024  # 16MB


def pack_message(msg_type: str, data: Any = None) -> bytes:
    """将消息类型和数据打包为字节"""
    body = {
        "type": msg_type,
        "data": data if data is not None else {}
    }
    json_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
    header = struct.pack(">I", len(json_bytes))
    return header + json_bytes


async def read_message(reader: asyncio.StreamReader) -> Optional[Message]:
    """从 StreamReader 读取一条消息"""
    try:
        header = await reader.readexactly(HEADER_SIZE)
    except asyncio.IncompleteReadError:
        return None

    length = struct.unpack(">I", header)[0]
    if length > MAX_MSG_SIZE:
        return None

    try:
        data = await reader.readexactly(length)
    except asyncio.IncompleteReadError:
        return None

    try:
        body = json.loads(data.decode("utf-8"))
        return Message(type=body["type"], data=body.get("data", {}))
    except (json.JSONDecodeError, KeyError):
        return None


async def write_message(writer: asyncio.StreamWriter, msg_type: str, data: Any = None):
    """向 StreamWriter 写入一条消息"""
    payload = pack_message(msg_type, data)
    writer.write(payload)
    await writer.drain()


def generate_run_id() -> str:
    """生成唯一的运行 ID"""
    return secrets.token_hex(8)


def generate_transaction_id() -> str:
    """生成事务 ID"""
    return secrets.token_hex(8)


def generate_sid() -> str:
    """生成会话 ID（用于 NAT 穿透配对）"""
    return secrets.token_hex(4)


def generate_token(n: int = 16) -> str:
    """生成随机 token"""
    return secrets.token_hex(n)


def addr_to_tuple(addr_str: str) -> tuple:
    """将 "host:port" 字符串转为 (host, port) 元组"""
    if ":" in addr_str:
        host, port = addr_str.rsplit(":", 1)
        return (host, int(port))
    return (addr_str, 0)