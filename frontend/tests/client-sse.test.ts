import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { AUTH_SESSION_EXPIRED_EVENT, streamAssistantChat, TOKEN_KEY } from '@/api/client'

const encoder = new TextEncoder()

function sseBody(chunks: string[]): ReadableStream<Uint8Array> {
  return new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk))
      controller.close()
    },
  })
}

function mockFetchWithBody(body: ReadableStream<Uint8Array>): ReturnType<typeof vi.fn> {
  return vi.fn(async () =>
    ({
      ok: true,
      status: 200,
      body,
    }) as unknown as Response,
  )
}

const donePayload = {
  answer: '完整回答',
  citations: [],
  trace: [],
  used_fallback: false,
  category: '一般咨询',
  quality_score: 0.9,
}

describe('streamAssistantChat SSE parsing', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('parses trace/token/reset/done events split across chunk boundaries', async () => {
    const fetchMock = mockFetchWithBody(
      sseBody([
        'event: trace\ndata: {"step":"知识检索","status":"completed"}\n\nev',
        'ent: token\ndata: {"text":"你好"}\n\nevent: token\ndata: {"text":"，世界',
        '"}\n\nevent: reset\ndata: {"text":"重置的回答"}\n\nevent: done\ndata: ' + JSON.stringify(donePayload) + '\n\n',
      ]),
    )
    vi.stubGlobal('fetch', fetchMock)

    const traces: string[] = []
    const tokens: string[] = []
    const resets: string[] = []
    const final = await streamAssistantChat(
      { message: '问题', mode: 'assistant' },
      {
        onTrace: (trace) => traces.push(trace.step),
        onToken: (text) => tokens.push(text),
        onReset: (text) => resets.push(text),
      },
    )

    expect(traces).toEqual(['知识检索'])
    expect(tokens).toEqual(['你好', '，世界'])
    expect(resets).toEqual(['重置的回答'])
    expect(final).toEqual(donePayload)
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect((init.body as string)).toContain('问题')
  })

  it('handles CRLF endings, comment lines and the optional space after the colon', async () => {
    const fetchMock = mockFetchWithBody(
      sseBody([
        ': keep-alive 注释行\r\n\r\nevent: token\r\ndata: {"text":"A"}\r\n\r\nevent: done\r\ndata: ' +
          JSON.stringify(donePayload) +
          '\r\n\r\n',
      ]),
    )
    vi.stubGlobal('fetch', fetchMock)

    const tokens: string[] = []
    const final = await streamAssistantChat(
      { message: '问题', mode: 'assistant' },
      { onTrace: () => undefined, onToken: (text) => tokens.push(text) },
    )

    expect(tokens).toEqual(['A'])
    expect(final).toEqual(donePayload)
  })

  it('ignores malformed JSON events and keeps the stream alive until done', async () => {
    const fetchMock = mockFetchWithBody(
      sseBody([
        'event: token\ndata: {"text": "第一"\ndata: "第二行"}\n\nevent: token\ndata: not-json\n\n' +
          'event: token\ndata: {"text":"有效"}\n\nevent: done\ndata: ' + JSON.stringify(donePayload) + '\n\n',
      ]),
    )
    vi.stubGlobal('fetch', fetchMock)

    const tokens: string[] = []
    const final = await streamAssistantChat(
      { message: '问题', mode: 'assistant' },
      { onTrace: () => undefined, onToken: (text) => tokens.push(text) },
    )

    // The multi-line data event and the not-json event are both skipped;
    // only the well-formed token after them reaches the handler.
    expect(tokens).toEqual(['有效'])
    expect(final).toEqual(donePayload)
  })

  it('rejects when the stream ends without a done event', async () => {
    vi.stubGlobal('fetch', mockFetchWithBody(sseBody(['event: token\ndata: {"text":"片段"}\n\n'])))

    await expect(
      streamAssistantChat(
        { message: '问题', mode: 'assistant' },
        { onTrace: () => undefined, onToken: () => undefined },
      ),
    ).rejects.toThrow('流式回答未返回完成事件。')
  })

  it('rejects with AuthenticationError and expires the session on 401', async () => {
    localStorage.setItem(TOKEN_KEY, 'expired-token')
    let expiredEventFired = false
    window.addEventListener(AUTH_SESSION_EXPIRED_EVENT, () => {
      expiredEventFired = true
    }, { once: true })

    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        ({
          ok: false,
          status: 401,
          json: async () => ({ detail: '登录已过期' }),
        }) as unknown as Response,
      ),
    )

    const promise = streamAssistantChat(
      { message: '问题', mode: 'assistant' },
      { onTrace: () => undefined, onToken: () => undefined },
    )
    await expect(promise).rejects.toThrow('登录已过期')
    await promise.catch((error: Error) => {
      expect(error.name).toBe('AuthenticationError')
    })
    expect(localStorage.getItem(TOKEN_KEY)).toBeNull()
    expect(expiredEventFired).toBe(true)
  })

  it('surfaces a timeout error when the provider stream stalls', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((_url: string, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener('abort', () => reject(new Error('aborted')))
        }),
      ),
    )

    await expect(
      streamAssistantChat(
        { message: '问题', mode: 'assistant' },
        { onTrace: () => undefined, onToken: () => undefined },
        { timeoutMs: 20 },
      ),
    ).rejects.toThrow('流式回答等待超时，请稍后重试。')
  })
})
