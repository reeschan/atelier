// jev.mjs のテスト。127.0.0.1 のモックサーバーだけを相手にする (本物の Jev は呼ばない)。
import { test } from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { callJev, getApiKey, JevError, DEFAULT_ENDPOINT } from "./jev.mjs";

const DUMMY_KEY = "dummy-test-key";
const OK_BODY = {
  model: "jev-1.13.0",
  answers: { k1: { type: "choice", choice: "blue", confidence: 0.82 } },
  usage: { input_tokens: 1, output_tokens: 1 },
};
const QUESTIONS = { k1: { type: "choice", instructions: "Which?", criteria: { blue: "b", red: "r" } } };
const MISSING_CRED = join(tmpdir(), "jev-test-no-such-dir", "credentials.json");

// 応答の列 [{status, headers, body}] を順に返す。尽きたら最後の応答を繰り返す
async function withMock(responses, fn) {
  const requests = [];
  const server = createServer((req, res) => {
    let raw = "";
    req.setEncoding("utf8");
    req.on("data", (c) => { raw += c; });
    req.on("end", () => {
      requests.push({ headers: req.headers, body: raw });
      const r = responses[Math.min(requests.length - 1, responses.length - 1)];
      const data = typeof r.body === "string" ? r.body : JSON.stringify(r.body);
      res.writeHead(r.status, { "content-type": "application/json", ...(r.headers ?? {}) });
      res.end(data);
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const url = `http://127.0.0.1:${server.address().port}/v1/systemone`;
  try {
    return await fn({ url, requests });
  } finally {
    server.closeAllConnections();
    await new Promise((r) => server.close(r));
  }
}

// process.env を一時的に書き換えて戻す
async function withEnv(changes, fn) {
  const saved = {};
  for (const k of Object.keys(changes)) saved[k] = process.env[k];
  try {
    for (const [k, v] of Object.entries(changes)) {
      if (v === undefined) delete process.env[k]; else process.env[k] = v;
    }
    return await fn();
  } finally {
    for (const [k, v] of Object.entries(saved)) {
      if (v === undefined) delete process.env[k]; else process.env[k] = v;
    }
  }
}

const noSleep = () => {
  const waits = [];
  return { waits, sleep: async (ms) => { waits.push(ms); } };
};

const call = (url, extra = {}) =>
  callJev({ state: "some text", questions: QUESTIONS, model: "jev-1.13.0", endpoint: url,
    apiKey: DUMMY_KEY, sleep: async () => {}, ...extra });

test("DEFAULT_ENDPOINT", () => {
  assert.equal(DEFAULT_ENDPOINT, "https://api.typesafe.ai/v1/systemone");
});

test("1. 200 の応答 JSON がそのまま返り、本文とヘッダが正しい", async () => {
  await withMock([{ status: 200, body: OK_BODY }], async ({ url, requests }) => {
    const result = await call(url);
    assert.deepEqual(result, OK_BODY);
    assert.equal(requests.length, 1);
    const sent = JSON.parse(requests[0].body);
    assert.equal(sent.state, "some text");
    assert.equal(sent.model, "jev-1.13.0");
    assert.deepEqual(sent.questions, QUESTIONS);
    assert.equal(requests[0].headers.authorization, `Bearer ${DUMMY_KEY}`);
    assert.equal(requests[0].headers["content-type"], "application/json");
  });
});

test("2. 429 (retry-after: 0) のあと 200 で成功、リクエストは 2 回", async () => {
  const { waits, sleep } = noSleep();
  await withMock([
    { status: 429, headers: { "retry-after": "0" }, body: { error: "rate" } },
    { status: 200, body: OK_BODY },
  ], async ({ url, requests }) => {
    assert.deepEqual(await call(url, { sleep }), OK_BODY);
    assert.equal(requests.length, 2);
    assert.deepEqual(waits, [0]);
  });
});

test("3. 401 は再試行しない", async () => {
  await withMock([{ status: 401, body: { error: "unauthorized" } }], async ({ url, requests }) => {
    await assert.rejects(call(url), (e) => {
      assert.ok(e instanceof JevError);
      assert.equal(e.code, "http");
      assert.equal(e.status, 401);
      assert.ok(!e.message.includes(DUMMY_KEY));
      return true;
    });
    assert.equal(requests.length, 1);
  });
});

test("4. 529 が続くとリクエスト 4 回で失敗", async () => {
  const { waits, sleep } = noSleep();
  await withMock([{ status: 529, body: { error: "overloaded" } }], async ({ url, requests }) => {
    await assert.rejects(call(url, { sleep }), (e) => e instanceof JevError && e.status === 529);
    assert.equal(requests.length, 4);
    assert.deepEqual(waits, [1000, 2000, 4000]);
  });
});

test("5. 500 が続くとリクエスト 2 回で失敗", async () => {
  const { waits, sleep } = noSleep();
  await withMock([{ status: 500, body: { error: "boom" } }], async ({ url, requests }) => {
    await assert.rejects(call(url, { sleep }), (e) => e.code === "http" && e.status === 500);
    assert.equal(requests.length, 2);
    assert.deepEqual(waits, []);
  });
});

test("6. キーが無ければ HTTP を呼ばずに no-key", async () => {
  await withEnv({ TYPESAFE_API_KEY: undefined, TYPESAFE_CREDENTIALS_FILE: MISSING_CRED }, async () => {
    assert.equal(getApiKey(), null);
    await withMock([{ status: 200, body: OK_BODY }], async ({ url, requests }) => {
      await assert.rejects(
        callJev({ state: "t", questions: QUESTIONS, model: "jev-1.13.0", endpoint: url, sleep: async () => {} }),
        (e) => e instanceof JevError && e.code === "no-key" && e.status === 0,
      );
      assert.equal(requests.length, 0);
    });
  });
});

test("6b. 資格情報ファイルのキーを使い、環境変数があればそちらが優先", async () => {
  const dir = mkdtempSync(join(tmpdir(), "jev-test-"));
  try {
    const cred = join(dir, "credentials.json");
    writeFileSync(cred, JSON.stringify({ api_key: "file-dummy" }));
    await withEnv({ TYPESAFE_API_KEY: undefined, TYPESAFE_CREDENTIALS_FILE: cred }, async () => {
      await withMock([{ status: 200, body: OK_BODY }], async ({ url, requests }) => {
        await callJev({ state: "t", questions: QUESTIONS, model: "jev-1.13.0", endpoint: url });
        assert.equal(requests[0].headers.authorization, "Bearer file-dummy");
      });
    });
    await withEnv({ TYPESAFE_API_KEY: "env-dummy", TYPESAFE_CREDENTIALS_FILE: cred }, async () => {
      await withMock([{ status: 200, body: OK_BODY }], async ({ url, requests }) => {
        await callJev({ state: "t", questions: QUESTIONS, model: "jev-1.13.0", endpoint: url });
        assert.equal(requests[0].headers.authorization, "Bearer env-dummy");
      });
    });
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("6c. getApiKey に env を渡せる", () => {
  assert.equal(getApiKey({ TYPESAFE_API_KEY: "x" }), "x");
  assert.equal(getApiKey({ TYPESAFE_CREDENTIALS_FILE: MISSING_CRED }), null);
});

test("7. answers が無い 200 は bad-response", async () => {
  await withMock([{ status: 200, body: { x: 1 } }], async ({ url, requests }) => {
    await assert.rejects(call(url), (e) => e instanceof JevError && e.code === "bad-response");
    assert.equal(requests.length, 1);
  });
});

test("7b. JSON でない 200 は bad-response", async () => {
  await withMock([{ status: 200, body: "not json" }], async ({ url }) => {
    await assert.rejects(call(url), (e) => e.code === "bad-response");
  });
});
