/* 测试台的 SSE 传输层：分片在任意位置切开也要正确累积，错误响应要抛出清理后的说明。
 * 运行：node tests/javascript/_test_playground_stream.js
 */
import assert from 'node:assert/strict';
import { StreamError, streamChat } from '../../app/web/js/pages/playground/stream.js';

const encoder = new TextEncoder();

function streamOf(chunks) {
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) {
        controller.enqueue(typeof chunk === 'string' ? encoder.encode(chunk) : chunk);
      }
      controller.close();
    },
  });
}

function responseOf(chunks, { ok = true, status = 200, text = '' } = {}) {
  return { ok, status, body: streamOf(chunks), text: async () => text };
}

const usage = { prompt_tokens: 12, completion_tokens: 34, total_tokens: 46, credit: 0.05 };
const sse = [
  'data: {"choices":[{"delta":{"reasoning_content":"先想"}}]}\n\n',
  'data: {"choices":[{"delta":{"content":"你' + '好"}}]}\n\n',
  'data: {"choices":[{"delta":{"content":"，世界"},"finish_reason":"stop"}]}\n\n',
  `data: ${JSON.stringify({ choices: [], usage })}\n\n`,
  'data: [DONE]\n\n',
].join('');

// 正常流：按 7 字节切片，模拟任意网络分片
const bytes = encoder.encode(sse);
const slices = [];
for (let i = 0; i < bytes.length; i += 7) slices.push(bytes.slice(i, i + 7));
const updates = [];
const answer = await streamChat({
  url: '/v1/chat/completions',
  headers: { 'X-Panel-Token': 't' },
  body: { model: 'glm-5.3' },
  fetchImpl: async (url, init) => {
    assert.equal(url, '/v1/chat/completions');
    assert.equal(init.method, 'POST');
    assert.equal(JSON.parse(init.body).model, 'glm-5.3');
    assert.equal(init.headers['X-Panel-Token'], 't');
    assert.equal(init.headers['Content-Type'], 'application/json');
    return responseOf(slices);
  },
  onUpdate: (partial) => updates.push(partial.content),
});
assert.equal(answer.content, '你好，世界');
assert.equal(answer.reasoning, '先想');
assert.equal(answer.finishReason, 'stop');
assert.equal(answer.usage.total, 46);
assert.equal(answer.usage.credit, 0.05);
assert.equal(answer.error, '');
assert.equal(answer.stopped, undefined);
assert.ok(updates.length >= 3, '每个分片都会通知一次');
assert.ok(updates.includes('你好'), updates.join('|'));

// 流里的错误分片
const errored = await streamChat({
  url: '/v1/chat/completions',
  headers: {},
  body: {},
  fetchImpl: async () => responseOf(['data: {"error":{"message":"upstream 503 overloaded"}}\n\n', 'data: [DONE]\n\n']),
});
assert.equal(errored.error, 'upstream 503 overloaded');
assert.equal(errored.content, '');

// 非 JSON 数据行按协议忽略
const noisy = await streamChat({
  url: '/x', headers: {}, body: {},
  fetchImpl: async () => responseOf(['event: ping\ndata: keep-alive\n\n', 'data: {"choices":[{"delta":{"content":"好"}}]}\n\n']),
});
assert.equal(noisy.content, '好');

// 非 2xx：抛出带状态与清理后说明的错误
await assert.rejects(
  () => streamChat({
    url: '/v1/chat/completions', headers: {}, body: {},
    fetchImpl: async () => responseOf([], {
      ok: false, status: 401,
      text: '<html><head><title>401 Authorization Required</title></head><body>x</body></html>',
    }),
  }),
  (err) => {
    assert.ok(err instanceof StreamError);
    assert.equal(err.status, 401);
    assert.equal(err.message, '401 Authorization Required x');
    return true;
  },
);

await assert.rejects(
  () => streamChat({
    url: '/v1/chat/completions', headers: {}, body: {},
    fetchImpl: async () => responseOf([], { ok: false, status: 503, text: '{"error":{"message":"网关并发已满"}}' }),
  }),
  (err) => err.status === 503 && err.message === '网关并发已满',
);

// 没有响应体
await assert.rejects(
  () => streamChat({ url: '/x', headers: {}, body: {}, fetchImpl: async () => ({ ok: true, status: 200, body: null }) }),
  (err) => err instanceof StreamError && /流式响应体/.test(err.message),
);

// 请求还没返回就中止：按已终止返回，不当成请求失败
{
  const controller = new AbortController();
  const stopped = await streamChat({
    url: '/v1/chat/completions', headers: {}, body: {}, signal: controller.signal,
    fetchImpl: async () => {
      controller.abort();
      throw new DOMException('The operation was aborted.', 'AbortError');
    },
  });
  assert.equal(stopped.stopped, true);
  assert.equal(stopped.content, '');
  assert.equal(stopped.error, '');
}

// 用户中止：保留已收到的内容并标记 stopped
{
  const controller = new AbortController();
  let push;
  const stream = new ReadableStream({ start(ctl) { push = ctl; } });
  const pending = streamChat({
    url: '/v1/chat/completions', headers: {}, body: {}, signal: controller.signal,
    fetchImpl: async () => ({ ok: true, status: 200, body: stream, text: async () => '' }),
  });
  push.enqueue(encoder.encode('data: {"choices":[{"delta":{"content":"半句"}}]}\n\n'));
  await new Promise((resolve) => setTimeout(resolve, 5));
  controller.abort();
  push.error(new DOMException('The operation was aborted.', 'AbortError'));
  const aborted = await pending;
  assert.equal(aborted.content, '半句');
  assert.equal(aborted.stopped, true);
}

console.log('playground stream checks passed');
