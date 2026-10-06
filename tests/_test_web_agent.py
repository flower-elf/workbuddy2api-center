"""网页打卡要真的把会话驱动起来，不能只建会话（issue #90）。

POST /console/as/conversations/ 只是排队，agent 要等客户端接上沙箱（ACP over HTTP + SSE）并请求这一轮才会跑；调用顺序与两种上报结果在此钉住。
"""
import http.client
import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import wb_webagent as W


class FakeChannel(object):
    """顶替 AcpChannel：记录 JSON-RPC 调用，不碰网络。"""

    def __init__(self, link, token, user_agent, log=None, proxy="", timeout=30):
        self.link = link
        self.token = token
        self.user_agent = user_agent
        self.proxy = proxy
        self.calls = []
        self.updates = 0
        self.chunks = 0
        self.closed = False
        self.fail_on = None

    def open(self):
        self.calls.append(("open",))
        return "conn-1"

    def request(self, method, params, request_id):
        if self.fail_on == method:
            raise W.AcpError("%s 返回 HTTP 500" % method)
        self.calls.append((method, params, request_id))
        if method == "session/prompt":
            self.updates, self.chunks = 7, 3
        return 202

    def close(self):
        self.closed = True


def run(statuses, channel_attrs=None, **kwargs):
    """用一段状态脚本跑 run_turn，返回 (结果, 通道)。"""
    made = []

    def factory(*args, **factory_kwargs):
        channel = FakeChannel(*args, **factory_kwargs)
        if channel_attrs:
            channel.__dict__.update(channel_attrs)
        made.append(channel)
        return channel

    remaining = list(statuses)

    def poll():
        return remaining.pop(0) if remaining else "working"

    result = W.run_turn("https://box.example/acp", "tok", "sess-1", "/workspace",
                        "Hi", "ua", poll_status=poll, poll_interval=0,
                        channel_factory=factory, **kwargs)
    return result, made[0]


class RunTurnTests(unittest.TestCase):
    def test_the_turn_is_driven_in_order(self):
        result, channel = run(["working", "completed"])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "completed")
        self.assertEqual([call[0] for call in channel.calls],
                         ["open", "initialize", "session/load", "session/prompt"])
        load = channel.calls[2][1]
        self.assertEqual(load["sessionId"], "sess-1")
        self.assertEqual(load["cwd"], "/workspace")
        self.assertEqual(load["mcpServers"], [])
        prompt = channel.calls[3][1]
        self.assertEqual(prompt["sessionId"], "sess-1")
        self.assertEqual(prompt["prompt"], [{"type": "text", "text": "Hi"}])
        self.assertTrue(channel.closed)
        self.assertEqual(result["events"], 7)
        self.assertEqual(result["chunks"], 3)

    def test_a_conversation_that_never_finishes_is_reported(self):
        result, channel = run(["working"], wait_seconds=1)
        self.assertFalse(result["ok"])
        self.assertIn("没有跑完", result["error"])
        self.assertTrue(channel.closed)

    def test_a_failed_conversation_stops_early(self):
        result, _channel = run(["working", "failed"])
        self.assertFalse(result["ok"])
        self.assertIn("failed", result["error"])

    def test_a_transport_error_is_reported_not_raised(self):
        result, channel = run(["working"], channel_attrs={"fail_on": "session/prompt"})
        self.assertFalse(result["ok"])
        self.assertIn("session/prompt", result["error"])
        self.assertTrue(channel.closed)


class SseParsingTests(unittest.TestCase):
    def test_updates_and_chunks_are_counted(self):
        channel = W.AcpChannel("https://box.example/acp", "tok", "ua")
        lines = []
        for message in (
            {"jsonrpc": "2.0", "method": "session/update",
             "params": {"update": {"sessionUpdate": "session_info_update"}}},
            {"jsonrpc": "2.0", "method": "session/update",
             "params": {"update": {"sessionUpdate": "agent_message_chunk",
                                   "content": {"type": "text", "text": "hi"}}}},
            {"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "end_turn"}},
        ):
            lines.append(("event: message\ndata: " + json.dumps(message) + "\n\n")
                          .encode("utf-8"))
        lines.append(b": heartbeat\n\n")

        channel._read_events(io.BytesIO(b"".join(lines)))
        drained = []
        while True:
            item = channel.events.get_nowait()
            if item is None:
                break
            drained.append(item)
        self.assertEqual(channel.updates, 2)
        self.assertEqual(channel.chunks, 1)
        self.assertEqual(len(drained), 3)
        self.assertEqual(drained[-1]["id"], 3)

    def test_a_data_line_split_across_chunks_is_parsed(self):
        # 真实 HTTPResponse：分块编码把第一条 data 行切在两个 chunk 之间
        def event(text):
            payload = {"jsonrpc": "2.0", "method": "session/update",
                       "params": {"update": {"sessionUpdate": "agent_message_chunk",
                                             "content": {"type": "text", "text": text}}}}
            return ("data: " + json.dumps(payload) + "\n\n").encode("utf-8")

        first = event("A")
        body = b"".join(b"%x\r\n%s\r\n" % (len(piece), piece)
                        for piece in (first[:25], first[25:], event("B")))
        raw = (b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
               b"Transfer-Encoding: chunked\r\n\r\n" + body + b"0\r\n\r\n")

        class FakeSocket(object):
            def makefile(self, mode, *args, **kwargs):
                return io.BufferedReader(io.BytesIO(raw))

        resp = http.client.HTTPResponse(FakeSocket())
        resp.begin()
        channel = W.AcpChannel("https://box.example/acp", "tok", "ua")
        channel._read_events(resp)
        texts = []
        while True:
            item = channel.events.get_nowait()
            if item is None:
                break
            texts.append(item["params"]["update"]["content"]["text"])
        self.assertEqual(texts, ["A", "B"])
        self.assertEqual(channel.chunks, 2)

    def test_the_client_capabilities_stay_honest(self):
        # 打卡不需要文件系统/终端：能力声明必须是关的，否则沙箱会等我们回调。
        self.assertFalse(W.CLIENT_CAPABILITIES["terminal"])
        self.assertFalse(W.CLIENT_CAPABILITIES["fs"]["readTextFile"])
        self.assertFalse(W.CLIENT_CAPABILITIES["fs"]["writeTextFile"])
        self.assertEqual(W.PROTOCOL_VERSION, 1)


if __name__ == "__main__":
    unittest.main()

