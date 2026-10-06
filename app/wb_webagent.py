# -*- coding: utf-8 -*-
"""把一条网页端（云 agent）会话真正驱动起来（ACP over streamable HTTP）。

issue #90：POST /console/as/conversations/ 只是**排队**了一条会话，agent 要等
客户端接上这条会话的沙箱（GET /console/as/conversations/{id}/session 返回的
`link`）并且请求这一轮才会跑。网页端的顺序：

  1. GET <link>，Accept: text/event-stream —— SSE 流，响应头里带 Acp-Connection-Id；
  2. POST <link>（带 Acp-Connection-Id），body 是 JSON-RPC 请求：
     initialize → session/load → session/prompt；
  3. 服务端把 session/update 通知推回 SSE 流里，直到这一轮结束。

只建会话、不接沙箱，会话会永远停在 CREATING 且没有任何输出。这里只实现打卡需要
的那点协议：不带工具、不接终端、不回调文件系统。
"""

import http.client
import json
import queue
import threading
import time
import urllib.parse

PROTOCOL_VERSION = 1

#: 打卡只需要 agent 自己把话说完，不需要客户端提供文件系统/终端能力。
CLIENT_CAPABILITIES = {
    "fs": {"readTextFile": False, "writeTextFile": False},
    "terminal": False,
}


class AcpError(Exception):
    """驱动失败时抛这个（调用方转成可读的错误文本）。"""


class AcpChannel(object):
    """ACP 的 streamable-HTTP 传输：一条 SSE 收事件，POST 发请求。"""

    def __init__(self, link, token, user_agent, log=None, proxy="", timeout=30):
        self.link = link
        self.token = token
        self.user_agent = user_agent
        self.log = log or (lambda *a, **k: None)
        self.proxy = proxy or ""
        self.timeout = timeout
        self.connection_id = ""
        self.events = queue.Queue()
        self.updates = 0
        self.chunks = 0
        self._conn = None
        self._reader = None
        self._stop = threading.Event()

    # ---- 连接 ----------------------------------------------------------
    def _target(self):
        parsed = urllib.parse.urlparse(self.link)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise AcpError("沙箱地址不可用：%r" % self.link)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return parsed, port

    def _connect(self):
        parsed, port = self._target()
        cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        conn = cls(parsed.hostname, port, timeout=self.timeout)
        if self.proxy:
            # 账号配了代理时走 CONNECT，和 urlopen 那条路径保持一致。
            proxy = urllib.parse.urlparse(self.proxy)
            conn = cls(proxy.hostname, proxy.port or 8080, timeout=self.timeout)
            conn.set_tunnel(parsed.hostname, port)
        return conn

    def open(self):
        """建立 SSE 通道，返回服务端给的 Acp-Connection-Id。"""
        parsed, _port = self._target()
        conn = self._connect()
        conn.putrequest("GET", parsed.path or "/")
        conn.putheader("Accept", "text/event-stream")
        conn.putheader("Authorization", "Bearer " + self.token)
        conn.putheader("User-Agent", self.user_agent)
        conn.endheaders()
        resp = conn.getresponse()
        if resp.status != 200:
            conn.close()
            raise AcpError("SSE 通道返回 HTTP %s" % resp.status)
        connection_id = resp.getheader("Acp-Connection-Id") or ""
        if not connection_id:
            conn.close()
            raise AcpError("SSE 通道没有返回 Acp-Connection-Id")
        self._conn = conn
        self.connection_id = connection_id
        self._reader = threading.Thread(target=self._read_events, args=(resp,), daemon=True)
        self._reader.start()
        return connection_id

    def _read_events(self, resp):
        """把 SSE 流解析成 JSON-RPC 消息，塞进 self.events。"""
        try:
            while not self._stop.is_set():
                # 走 HTTPResponse 自己的 readline：分块编码在行中间切分时也能正确拼回
                line = resp.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                if not text.startswith("data:"):
                    continue
                payload = text[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    message = json.loads(payload)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                if message.get("method") == "session/update":
                    update = (message.get("params") or {}).get("update") or {}
                    self.updates += 1
                    if update.get("sessionUpdate") == "agent_message_chunk":
                        self.chunks += 1
                self.events.put(message)
        except Exception as exc:      # 连接被关掉、超时等
            self.events.put({"__stream_error__": "%s: %s" % (type(exc).__name__, exc)})
        finally:
            self.events.put(None)     # 流结束的哨兵

    def request(self, method, params, request_id):
        """发一个 JSON-RPC 请求；成功返回 HTTP 状态码（正常是 202）。"""
        parsed, _port = self._target()
        body = json.dumps({"jsonrpc": "2.0", "id": request_id,
                           "method": method, "params": params}).encode("utf-8")
        conn = self._connect()
        try:
            conn.putrequest("POST", parsed.path or "/")
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Accept", "application/json, text/event-stream")
            conn.putheader("Acp-Connection-Id", self.connection_id)
            conn.putheader("Authorization", "Bearer " + self.token)
            conn.putheader("User-Agent", self.user_agent)
            conn.putheader("Content-Length", str(len(body)))
            conn.endheaders()
            conn.send(body)
            resp = conn.getresponse()
            resp.read()
            status = resp.status
        finally:
            conn.close()
        if status not in (200, 202):
            raise AcpError("%s 返回 HTTP %s" % (method, status))
        return status

    def close(self):
        self._stop.set()
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def run_turn(link, token, session_id, cwd, prompt, user_agent, poll_status=None,
             wait_seconds=120, poll_interval=3, log=None, proxy="",
             channel_factory=AcpChannel):
    """接上沙箱、把这一轮跑完。

    poll_status 是调用方给的「这条会话现在什么状态」（查 console API）；看到
    completed 就算跑完，failed/error 直接停。返回结果里带事件数与输出段数。
    """
    log = log or (lambda *a, **k: None)
    started = time.time()
    channel = channel_factory(link, token, user_agent, log=log, proxy=proxy)
    status = ""
    error = ""
    finished = False
    try:
        channel.open()
        channel.request("initialize",
                        {"protocolVersion": PROTOCOL_VERSION,
                         "clientCapabilities": CLIENT_CAPABILITIES}, 1)
        channel.request("session/load",
                        {"sessionId": session_id, "cwd": cwd or "/workspace",
                         "mcpServers": []}, 2)
        channel.request("session/prompt",
                        {"sessionId": session_id,
                         "prompt": [{"type": "text", "text": prompt}]}, 3)
        deadline = time.time() + max(1, int(wait_seconds))
        while time.time() < deadline:
            if poll_status is not None:
                try:
                    status = str(poll_status() or "")
                except Exception as exc:
                    log("web agent: 状态查询失败（%s）" % exc)
                if status == "completed":
                    finished = True
                    break
                if status in ("failed", "error"):
                    error = "会话状态=%s" % status
                    break
            if poll_interval:
                time.sleep(poll_interval)
    except AcpError as exc:
        error = str(exc)
    except Exception as exc:
        error = "%s: %s" % (type(exc).__name__, exc)
    finally:
        channel.close()
    elapsed_ms = int((time.time() - started) * 1000)
    if not status and poll_status is not None:
        try:
            status = str(poll_status() or "")
        except Exception:
            pass
    if not finished and not error:
        error = "会话在 %ss 内没有跑完（状态=%s）" % (wait_seconds, status or "未知")
    ok = finished and not error
    return {"ok": ok, "status": status, "events": channel.updates,
            "chunks": channel.chunks, "elapsed_ms": elapsed_ms, "error": error}
