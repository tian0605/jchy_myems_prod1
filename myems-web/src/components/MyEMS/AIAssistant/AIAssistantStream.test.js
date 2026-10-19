import { TextDecoder, TextEncoder } from 'util';
import { requestAIStream } from './AIAssistantStream';

const encoder = new TextEncoder();
let chunks;
let reader;
let signal;
let originalFetch;
let originalDecoder;

beforeEach(() => {
  jest.useFakeTimers();
  originalFetch = global.fetch;
  originalDecoder = global.TextDecoder;
  global.TextDecoder = TextDecoder;
  chunks = [];
  reader = {
    read: jest.fn(() => {
      if (chunks.length) return Promise.resolve({ done: false, value: encoder.encode(chunks.shift()) });
      return new Promise((resolve, reject) => {
        const abort = () => {
          const error = new Error('aborted');
          error.name = 'AbortError';
          reject(error);
        };
        signal.addEventListener('abort', abort, { once: true });
        if (signal.aborted) abort();
      });
    }),
    releaseLock: jest.fn()
  };
  global.fetch = jest.fn((url, options) => {
    signal = options.signal;
    return Promise.resolve({ ok: true, body: { getReader: () => reader } });
  });
});

afterEach(() => {
  global.fetch = originalFetch;
  global.TextDecoder = originalDecoder;
  jest.useRealTimers();
});

test('done releases UI without waiting for HTTP EOF', async () => {
  chunks.push('event: delta\ndata: {"content":"answer"}\n\nevent: done\ndata: {}\n\n');
  const event = jest.fn();
  await requestAIStream('/synthetic', {}, event);
  expect(event).toHaveBeenCalledWith('done', {});
  expect(reader.read).toHaveBeenCalledTimes(1);
  expect(signal.aborted).toBe(true);
  expect(reader.releaseLock).toHaveBeenCalled();
  expect(jest.getTimerCount()).toBe(0);
});

test.each([60, 180])('metadata %ss deadline aborts a silent stream and exposes timeout', async seconds => {
  chunks.push(`event: meta\ndata: {"timeout_seconds":${seconds}}\n\n`,
    'event: status\ndata: {"stage":"waiting_for_model"}\n\n',
    'event: status\ndata: {"stage":"model_reasoning"}\n\n');
  const pending = requestAIStream('/synthetic', {}, jest.fn());
  const assertion = expect(pending).rejects.toThrow('AI response timed out');
  for (let i = 0; i < 10; i++) await Promise.resolve();
  jest.advanceTimersByTime((seconds + 5) * 1000 - 1);
  expect(signal.aborted).toBe(false);
  jest.advanceTimersByTime(1);
  await assertion;
  expect(signal.aborted).toBe(true);
});

test('error preserves server explanation and closes connection', async () => {
  chunks.push('event: error\ndata: {"message":"model unavailable"}\n\n');
  await expect(requestAIStream('/synthetic', {}, jest.fn())).rejects.toThrow('model unavailable');
  expect(signal.aborted).toBe(true);
});

test('EOF without done is not successful', async () => {
  reader.read.mockResolvedValue({ done: true });
  await expect(requestAIStream('/synthetic', {}, jest.fn())).rejects.toThrow('before completion');
});

test('handles split CRLF event delimiters', async () => {
  chunks.push('event: done\r\ndata: {}\r', '\n\r', '\n');
  await requestAIStream('/synthetic', {}, jest.fn());
  expect(reader.read).toHaveBeenCalledTimes(3);
});

test('stop waiting aborts the pending read', async () => {
  const external = new AbortController();
  const pending = requestAIStream('/synthetic', { signal: external.signal }, jest.fn());
  const assertion = expect(pending).rejects.toMatchObject({ name: 'AbortError' });
  await Promise.resolve();
  external.abort();
  await assertion;
  expect(jest.getTimerCount()).toBe(0);
});
