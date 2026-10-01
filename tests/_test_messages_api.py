"""The Anthropic Messages endpoint (/v1/messages) and the two pipelines.

A Messages request is translated down to whatever the settings page selected
(旧版 openai-completions 或新版 openai-responses) and the reply is translated
back into Anthropic shapes, so the checks are split by direction:

  * the request converters keep system / tools / tool_result / thinking;
  * the reply converters rebuild text, thinking and tool_use blocks;
  * both streaming pipelines emit one message_start .. message_stop sequence
    and hand the tool input over as valid JSON;
  * the handler picks the pipeline the setting names and answers errors in the
    Anthropic envelope instead of the OpenAI one.

Everything runs offline: the upstream is a fake iterable, the handler is a
stub carrying the real methods.
"""

import json
import os
import sys
import tempfile
import unittest

from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
_TMP = tempfile.mkdtemp(prefix="wb-messages-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP

import wb_proxy as proxy
import wb_settings as settings


def sse(chunks):
    out = [("data: " + json.dumps(c) + "\n\n").encode("utf-8") for c in chunks]
    out.append(b"data: [DONE]\n\n")
    return out


class FakeUpstream(object):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def __iter__(self):
        return iter(self.chunks)

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class FakeAccount(object):
    uid = "acct-test"


class ChatChunks(object):
    """A chat stream that thinks, talks and then calls one tool."""

    @staticmethod
    def tool_call():
        return [
            {"choices": [{"delta": {"reasoning_content": "let me check"}}]},
            {"choices": [{"delta": {"content": "Hello"}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call_a",
                 "function": {"name": "get_weather", "arguments": "{\"city\":"}}]}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": " \"SF\"}"}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}],
             "usage": {"prompt_tokens": 111, "completion_tokens": 22,
                       "total_tokens": 133}},
        ]

    @staticmethod
    def plain_text():
        return [
            {"choices": [{"delta": {"content": "Hi there"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12}},
        ]


class MessagesHandler(object):
    """Enough of the real Handler to drive /v1/messages offline."""

    _handle_messages = proxy.Handler._handle_messages
    _handle_messages_via_chat = proxy.Handler._handle_messages_via_chat
    _handle_messages_via_responses = proxy.Handler._handle_messages_via_responses
    _open_messages_upstream = proxy.Handler._open_messages_upstream
    _anthropic_error = proxy.Handler._anthropic_error
    _messages_stream_headers = proxy.Handler._messages_stream_headers
    _messages_stream_close = proxy.Handler._messages_stream_close
    _messages_stream_aborted = proxy.Handler._messages_stream_aborted
    _messages_stream_from_chat = proxy.Handler._messages_stream_from_chat
    _messages_nonstream_from_chat = proxy.Handler._messages_nonstream_from_chat
    _messages_stream_from_responses = proxy.Handler._messages_stream_from_responses
    _messages_nonstream_from_responses = proxy.Handler._messages_nonstream_from_responses

    class _WFile(object):
        def __init__(self, sink):
            self.sink = sink

        def write(self, data):
            self.sink.append(data)

        def flush(self):
            pass

    def __init__(self, payload):
        self.payload = payload
        self.path = "/v1/messages"
        self.headers = {}
        self.status = []
        self.written = []
        self.reply = None

    @property
    def wfile(self):
        return MessagesHandler._WFile(self.written)

    def send_response(self, code):
        self.status.append(("status", code))

    def send_header(self, name, value):
        self.status.append((name, value))

    def end_headers(self):
        self.status.append(("end", None))

    def _handle_expect_continue(self):
        pass

    def _discard_body(self):
        pass

    def _json(self, code, obj):
        self.status.append(("status", code))
        self.written.append(json.dumps(obj, ensure_ascii=False).encode("utf-8"))
        self.reply = obj
        return code, obj

    def _error(self, code, message, err_type="server_error"):
        return self._json(code, {"error": {"message": message, "type": err_type}})

    def _request_realm(self, explicit=None):
        return None

    def _cross_realm_error(self, model, realm):
        return ""

    def _banned_model_error(self, model):
        return ""

    def _key_model_error(self, model):
        return ""


def frames_of(handler):
    """The SSE frames the handler wrote, as (event, payload) pairs."""
    out = []
    text = b"".join(handler.written).decode("utf-8", "replace")
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        event, payload = "", None
        for line in block.split("\n"):
            if line.startswith("event: "):
                event = line[7:].strip()
            elif line.startswith("data: "):
                payload = json.loads(line[6:])
        out.append((event, payload))
    return out


def base_request(**overrides):
    body = {
        "model": "deepseek-v4.1-flash",
        "max_tokens": 1024,
        "system": "be brief",
        "messages": [{"role": "user", "content": "hi"}],
    }
    body.update(overrides)
    return body


def run_handler(payload, chunks, mode="openai-completions"):
    """Drive one full request through the stub handler against fake chunks."""
    with tempfile.TemporaryDirectory(prefix="wb-messages-dir-") as directory:
        settings.set_messages_format(directory, mode)
        handler = MessagesHandler(payload)
        with mock.patch.multiple(proxy, ACCOUNTS_DIR=directory,
                                 open_upstream=lambda body, session_key=None, target_realm=None:
                                     (FakeUpstream(sse(chunks)), FakeAccount()),
                                 record_usage=lambda *a, **k: None,
                                 record_error=lambda *a, **k: None):
            handler._handle_messages(payload)
        return handler


class ConvertersTest(unittest.TestCase):
    def test_system_and_tools_and_tool_results_translate(self):
        payload = base_request(
            system=[{"type": "text", "text": "line one"},
                    {"type": "text", "text": "line two"}],
            messages=[
                {"role": "user", "content": [
                    {"type": "text", "text": "go"},
                    {"type": "image", "source": {"type": "base64",
                                                 "media_type": "image/png",
                                                 "data": "AAAA"}}]},
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "toolu_1", "name": "get_weather",
                     "input": {"city": "SF"}}]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1",
                     "content": [{"type": "text", "text": "18C"}]}]},
            ],
            tools=[{"name": "get_weather", "description": "w",
                    "input_schema": {"type": "object", "properties": {}}}],
            tool_choice={"type": "any", "disable_parallel_tool_use": True},
        )
        chat = proxy.messages_to_chat(payload)
        self.assertEqual(chat["messages"][0],
                         {"role": "system", "content": "line one\nline two"})
        self.assertEqual(chat["messages"][1]["content"][1]["type"], "image_url")
        self.assertEqual(chat["messages"][1]["content"][1]["image_url"]["url"],
                         "data:image/png;base64,AAAA")
        call = chat["messages"][2]["tool_calls"][0]
        self.assertEqual(call["id"], "toolu_1")
        self.assertEqual(json.loads(call["function"]["arguments"]), {"city": "SF"})
        self.assertEqual(chat["messages"][3],
                         {"role": "tool", "tool_call_id": "toolu_1", "content": "18C"})
        self.assertEqual(chat["tools"][0]["function"]["name"], "get_weather")
        self.assertEqual(chat["tool_choice"], "required")
        self.assertFalse(chat["parallel_tool_calls"])

    def test_thinking_becomes_an_effort_level(self):
        payload = base_request(thinking={"type": "enabled", "budget_tokens": 2048})
        chat = proxy.messages_to_chat(payload)
        self.assertEqual(chat["thinking"], {"type": "enabled"})
        self.assertEqual(chat["reasoning_effort"], "low")
        big = proxy.messages_to_chat(
            base_request(thinking={"type": "enabled", "budget_tokens": 40000}))
        self.assertEqual(big["reasoning_effort"], "high")
        off = proxy.messages_to_chat(base_request(thinking={"type": "disabled"}))
        self.assertNotIn("thinking", off)

    def test_responses_body_mirrors_the_chat_one(self):
        payload = base_request(
            messages=[
                {"role": "user", "content": "go"},
                {"role": "assistant", "content": [
                    {"type": "text", "text": "sure"},
                    {"type": "tool_use", "id": "toolu_1", "name": "get_weather",
                     "input": {"city": "SF"}}]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "18C"}]},
            ],
            tools=[{"name": "get_weather", "description": "w",
                    "input_schema": {"type": "object", "properties": {}}}],
        )
        responses = proxy.messages_to_responses(payload)
        self.assertEqual(responses["instructions"], "be brief")
        self.assertEqual(responses["max_output_tokens"], 1024)
        kinds = [item["type"] for item in responses["input"]]
        self.assertEqual(kinds, ["message", "message", "function_call",
                                 "function_call_output"])
        self.assertEqual(responses["tools"][0]["name"], "get_weather")
        # the Responses pipeline turns it into the chat body the upstream gets
        chat = proxy.responses_to_chat(responses)
        self.assertEqual(chat["messages"][-1]["role"], "tool")
        self.assertEqual(chat["messages"][-1]["content"], "18C")
        assistant = [m for m in chat["messages"] if m.get("tool_calls")][0]
        self.assertEqual(assistant["tool_calls"][0]["id"], "toolu_1")

    def test_reply_folding(self):
        chat_obj = {
            "choices": [{"finish_reason": "tool_calls", "message": {
                "content": "ok", "reasoning_content": "hmm",
                "tool_calls": [{"id": "call_a", "type": "function",
                                "function": {"name": "get_weather",
                                             "arguments": "{\"city\": \"SF\"}"}}]}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 7},
        }
        message = proxy.chat_to_message(chat_obj, "m")
        self.assertEqual([b["type"] for b in message["content"]],
                         ["thinking", "text", "tool_use"])
        self.assertEqual(message["content"][2]["input"], {"city": "SF"})
        self.assertEqual(message["stop_reason"], "tool_use")
        self.assertEqual(message["usage"],
                         {"input_tokens": 5, "output_tokens": 7})

        responses_obj = {
            "status": "completed",
            "output": [
                {"type": "reasoning",
                 "summary": [{"type": "summary_text", "text": "hmm"}]},
                {"type": "message", "role": "assistant", "content": [
                    {"type": "output_text", "text": "ok"}]},
                {"type": "function_call", "call_id": "call_a", "name": "get_weather",
                 "arguments": "{\"city\": \"SF\"}"},
            ],
            "usage": {"input_tokens": 5, "output_tokens": 7},
        }
        message = proxy.response_to_message(responses_obj, "m")
        self.assertEqual([b["type"] for b in message["content"]],
                         ["thinking", "text", "tool_use"])
        self.assertEqual(message["stop_reason"], "tool_use")
        incomplete = proxy.response_to_message(
            {"status": "incomplete", "output": [], "usage": {}}, "m")
        self.assertEqual(incomplete["stop_reason"], "max_tokens")


class StreamPipelinesTest(unittest.TestCase):
    def test_chat_stream_builds_one_message(self):
        writer = proxy.MessageStreamWriter("m", input_tokens=99)
        frames = list(writer.start())
        frames += list(proxy.stream_chat_to_message(
            writer, FakeUpstream(sse(ChatChunks.tool_call()))))
        frames += list(writer.finish())
        text = b"".join(frames).decode("utf-8")

        events = [list(proxy._parse_sse_frame(f))[0]
                  for f in text.encode("utf-8").split(b"\n\n") if f.strip()]
        self.assertEqual(events[0], "message_start")
        self.assertEqual(events[-1], "message_stop")
        self.assertEqual(text.count("event: content_block_start"), 3)

        parsed = [json.loads(line[6:]) for line in text.split("\n")
                  if line.startswith("data: ")]
        starts = [p for p in parsed if p["type"] == "content_block_start"]
        self.assertEqual([s["content_block"]["type"] for s in starts],
                         ["thinking", "text", "tool_use"])
        deltas = [p for p in parsed if p["type"] == "content_block_delta"]
        self.assertEqual("".join(d["delta"]["thinking"] for d in deltas
                                 if d["delta"]["type"] == "thinking_delta"),
                         "let me check")
        self.assertEqual("".join(d["delta"]["text"] for d in deltas
                                 if d["delta"]["type"] == "text_delta"), "Hello")
        tool_json = "".join(d["delta"]["partial_json"] for d in deltas
                            if d["delta"]["type"] == "input_json_delta")
        self.assertEqual(json.loads(tool_json), {"city": "SF"})
        final = [p for p in parsed if p["type"] == "message_delta"][0]
        self.assertEqual(final["delta"]["stop_reason"], "tool_use")
        self.assertEqual(final["usage"], {"input_tokens": 111, "output_tokens": 22})

    def test_responses_stream_recovers_a_dsml_tool_call(self):
        dsml = ("<|DSML|tool_calls><|DSML|invoke name=\"get_weather\">"
                "<|DSML|parameter name=\"city\">SF</|DSML|parameter>"
                "</|DSML|invoke></|DSML|tool_calls>")
        chunks = [
            {"choices": [{"delta": {"content": dsml}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 50, "completion_tokens": 9, "total_tokens": 59}},
        ]
        writer = proxy.MessageStreamWriter("m", input_tokens=1)
        out = list(writer.start())
        holder = {"usage": None, "custom_names": set(), "request_meta": {},
                  "namespace_map": {}, "base_body": {}, "base_messages": [],
                  "session_key": "s", "realm": "intl"}
        out += list(proxy.stream_responses_to_message(
            writer, proxy.stream_responses_events(sse(chunks), "m", holder)))
        writer.usage = holder.get("usage")
        out += list(writer.finish())
        text = b"".join(out).decode("utf-8")

        starts = [json.loads(l[6:]) for l in text.split("\n") if l.startswith("data: ")]
        block_types = [p["content_block"]["type"] for p in starts
                       if p["type"] == "content_block_start"]
        self.assertEqual(block_types, ["tool_use"])
        self.assertNotIn("DSML", text)
        tool_json = "".join(
            p["delta"]["partial_json"] for p in starts
            if p["type"] == "content_block_delta"
            and p["delta"]["type"] == "input_json_delta")
        self.assertEqual(json.loads(tool_json), {"city": "SF"})
        final = [p for p in starts if p["type"] == "message_delta"][0]
        self.assertEqual(final["delta"]["stop_reason"], "tool_use")


class EndpointTest(unittest.TestCase):
    def test_chat_mode_nonstream_reply(self):
        chunks = ChatChunks.tool_call()
        handler = run_handler(base_request(), chunks)
        self.assertIn(("status", 200), handler.status)
        self.assertEqual([b["type"] for b in handler.reply["content"]],
                         ["thinking", "text", "tool_use"])
        self.assertEqual(handler.reply["stop_reason"], "tool_use")
        self.assertEqual(handler.reply["type"], "message")
        self.assertEqual(handler.reply["role"], "assistant")
        self.assertEqual(handler.reply["usage"]["input_tokens"], 111)

    def test_chat_mode_stream_reply(self):
        handler = run_handler(base_request(stream=True), ChatChunks.plain_text())
        self.assertIn(("status", 200), handler.status)
        frames = frames_of(handler)
        self.assertEqual(frames[0][0], "message_start")
        self.assertEqual(frames[-1][0], "message_stop")
        self.assertEqual(frames[0][1]["message"]["role"], "assistant")
        # message_start carries an estimate; the real counts arrive in message_delta
        self.assertGreater(frames[0][1]["message"]["usage"]["input_tokens"], 0)
        text = "".join(p["delta"]["text"] for _, p in frames
                       if p and p.get("type") == "content_block_delta")
        self.assertEqual(text, "Hi there")
        final = [p for _, p in frames if p and p.get("type") == "message_delta"][0]
        self.assertEqual(final["delta"]["stop_reason"], "end_turn")
        self.assertEqual(final["usage"], {"input_tokens": 9, "output_tokens": 3})

    def test_responses_mode_stream_reply(self):
        handler = run_handler(base_request(stream=True), ChatChunks.plain_text(),
                              mode="openai-responses")
        frames = frames_of(handler)
        self.assertEqual(frames[0][0], "message_start")
        self.assertEqual(frames[-1][0], "message_stop")
        text = "".join(p["delta"]["text"] for _, p in frames
                       if p and p.get("type") == "content_block_delta")
        self.assertEqual(text, "Hi there")

    def test_responses_mode_nonstream_reply(self):
        handler = run_handler(base_request(), ChatChunks.plain_text(),
                              mode="openai-responses")
        self.assertEqual([b["type"] for b in handler.reply["content"]], ["text"])
        self.assertEqual(handler.reply["content"][0]["text"], "Hi there")
        self.assertEqual(handler.reply["stop_reason"], "end_turn")

    def test_the_setting_selects_the_pipeline(self):
        payload = base_request()
        with tempfile.TemporaryDirectory(prefix="wb-messages-dir-") as directory:
            settings.set_messages_format(directory, "openai-responses")
            handler = MessagesHandler(payload)
            calls = []
            with mock.patch.multiple(proxy, ACCOUNTS_DIR=directory,
                                     record_usage=lambda *a, **k: None,
                                     record_error=lambda *a, **k: None), \
                    mock.patch.object(MessagesHandler, "_handle_messages_via_responses",
                                      lambda self, body: calls.append("responses")), \
                    mock.patch.object(MessagesHandler, "_handle_messages_via_chat",
                                      lambda self, body: calls.append("chat")):
                handler._handle_messages(payload)
            self.assertEqual(calls, ["responses"])

    def test_missing_max_tokens_is_refused(self):
        payload = base_request()
        payload.pop("max_tokens")
        handler = MessagesHandler(payload)
        handler._handle_messages(payload)
        self.assertEqual(handler.status[0], ("status", 400))
        body = json.loads(b"".join(handler.written).decode("utf-8"))
        self.assertEqual(body["type"], "error")
        self.assertEqual(body["error"]["type"], "invalid_request_error")

    def test_a_rate_limited_upstream_answers_in_the_anthropic_envelope(self):
        payload = base_request()
        handler = MessagesHandler(payload)

        def refuse(body, session_key=None, target_realm=None):
            raise proxy.RateLimited("slow down", detail="reset at 12:00", wait=42)

        with tempfile.TemporaryDirectory(prefix="wb-messages-dir-") as directory:
            settings.set_messages_format(directory, "openai-completions")
            with mock.patch.multiple(proxy, ACCOUNTS_DIR=directory,
                                     open_upstream=refuse,
                                     record_usage=lambda *a, **k: None,
                                     record_error=lambda *a, **k: None):
                handler._handle_messages(payload)
        self.assertEqual(handler.status[0], ("status", 429))
        self.assertIn(("Retry-After", "42"), handler.status)
        body = json.loads(b"".join(handler.written).decode("utf-8"))
        self.assertEqual(body["type"], "error")
        self.assertEqual(body["error"]["type"], "rate_limit_error")


class SettingsRoundTripTest(unittest.TestCase):
    def test_default_and_round_trip(self):
        with tempfile.TemporaryDirectory(prefix="wb-messages-set-") as directory:
            self.assertEqual(settings.messages_format(directory),
                             settings.DEFAULT_MESSAGES_FORMAT)
            self.assertEqual(settings.set_messages_format(directory, "openai-responses"),
                             "openai-responses")
            self.assertEqual(settings.messages_format(directory), "openai-responses")

    def test_unknown_values_fall_back_instead_of_sticking(self):
        with tempfile.TemporaryDirectory(prefix="wb-messages-set-") as directory:
            settings.set_messages_format(directory, "openai-responses")
            settings.save(directory, dict(settings.load(directory),
                                          messages_format="chat-completions"))
            self.assertEqual(settings.messages_format(directory),
                             settings.DEFAULT_MESSAGES_FORMAT)
            self.assertEqual(settings.set_messages_format(directory, "nonsense"),
                             settings.DEFAULT_MESSAGES_FORMAT)

    def test_the_save_route_validates_and_reports(self):
        with tempfile.TemporaryDirectory(prefix="wb-messages-set-") as directory:
            handler = MessagesHandler(None)
            handler._payload_or_error = lambda allow_list=False: {
                "messages_format": "openai-responses"}
            with mock.patch.multiple(proxy, ACCOUNTS_DIR=directory, POOL=None,
                                     SCHEDULER=None):
                code, reply = proxy.Handler._handle_settings_save(handler)
            self.assertEqual(code, 200)
            self.assertEqual(reply["messages_format"], "openai-responses")
            self.assertEqual(settings.messages_format(directory), "openai-responses")

            handler._payload_or_error = lambda allow_list=False: {
                "messages_format": "openai-chat"}
            with mock.patch.multiple(proxy, ACCOUNTS_DIR=directory, POOL=None,
                                     SCHEDULER=None):
                code, body = proxy.Handler._handle_settings_save(handler)
            self.assertEqual(code, 400)
            self.assertEqual(settings.messages_format(directory), "openai-responses")

    def test_runtime_view_exposes_the_choice(self):
        with tempfile.TemporaryDirectory(prefix="wb-messages-set-") as directory:
            settings.set_messages_format(directory, "openai-responses")
            with mock.patch.object(proxy, "ACCOUNTS_DIR", directory):
                view = proxy.runtime_settings_view()
            self.assertEqual(view["messages_format"], "openai-responses")
            self.assertEqual(view["messages_format_default"], "openai-completions")


if __name__ == "__main__":
    unittest.main(verbosity=2)
