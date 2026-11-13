// 共通 Jev クライアント (JS 版)。キーの読み方・再試行の決まりは README.md を参照。
// resolveApiKey は見つけた場所か失敗の理由を返す。publicCode は HTTP を http-<status> にする。
import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

export const DEFAULT_ENDPOINT = "https://api.typesafe.ai/v1/systemone";

export class JevError extends Error {
  // code: "no-key" | "http" | "network" | "timeout" | "bad-response"
  // status: HTTP ステータス(無ければ 0)
  // where: キーを見つけた場所か、no-key の理由。キーの値は入れない
  constructor(message, { code, status = 0, where = null }) {
    super(message);
    this.name = "JevError";
    this.code = code;
    this.status = status;
    this.where = where;
  }
}

function credentialsPath(env) {
  const explicit = env.TYPESAFE_CREDENTIALS_FILE;
  if (typeof explicit === "string" && explicit) return { path: explicit, explicit: true };
  const home = (typeof env.HOME === "string" && env.HOME)
    || (typeof env.USERPROFILE === "string" && env.USERPROFILE)
    || homedir();
  return { path: join(home, ".config", "typesafe", "credentials.json"), explicit: false };
}

function readJsonFile(path) {
  // utf-8-sig と同じ。BOM があっても JSON として読む
  let text = readFileSync(path, "utf8");
  if (text.charCodeAt(0) === 0xfeff) text = text.slice(1);
  return JSON.parse(text);
}

function missingFile(path, explicit) {
  return explicit ? `ファイル無し: ${path}` : "env無し";
}

// 見つけた場所か、失敗の理由を返す。key は無ければ null。getApiKey は key だけを返す
export function resolveApiKey(env = process.env) {
  const fromEnv = env.TYPESAFE_API_KEY;
  if (typeof fromEnv === "string" && fromEnv) return { key: fromEnv, where: "env" };
  const { path, explicit } = credentialsPath(env);
  if (!existsSync(path)) return { key: null, where: missingFile(path, explicit) };
  let data;
  try {
    data = readJsonFile(path);
  } catch (e) {
    if (e && (e.code === "ENOENT" || e.code === "ENOTDIR")) {
      return { key: null, where: missingFile(path, explicit) };
    }
    return { key: null, where: `JSONが壊れている: ${path}` };
  }
  const key = data && typeof data === "object" && !Array.isArray(data) ? data.api_key : null;
  if (typeof key === "string" && key) return { key, where: path };
  return { key: null, where: `api_keyが無い: ${path}` };
}

// 1.0 節の順で探す。無ければ null
export function getApiKey(env = process.env) {
  return resolveApiKey(env).key;
}

// CLI に出す code。HTTP は http-<status>（401 と 403 をほかと分ける）
export function publicCode(err) {
  if (err && err.code === "http" && err.status) return `http-${err.status}`;
  return err ? err.code : undefined;
}

function retryWaitMs(retryAfter, n) {
  let seconds = retryAfter == null || retryAfter === "" ? NaN : Number(retryAfter);
  if (!Number.isFinite(seconds) || seconds < 0) seconds = 2 ** n;
  return Math.min(seconds, 30) * 1000;
}

// 応答 JSON(オブジェクト)を返す。失敗は JevError を投げる
export async function callJev({ state, questions, model, endpoint = DEFAULT_ENDPOINT,
  apiKey, timeoutMs = 120000, maxRetries = 3,
  sleep = (ms) => new Promise((r) => setTimeout(r, ms)) }) {
  let where = null;
  if (apiKey === undefined) {
    const found = resolveApiKey();
    apiKey = found.key;
    where = found.where;
  }
  const redact = (text) => (apiKey && text ? String(text).split(apiKey).join("[redacted]") : text);
  if (!apiKey) throw new JevError("Jev の API キーが見つからない", { code: "no-key", where });
  const body = JSON.stringify({ state, model, questions });
  const headers = { Authorization: `Bearer ${apiKey}`, "Content-Type": "application/json" };

  let rateRetries = 0;
  let otherRetried = false;
  for (;;) {
    let res;
    try {
      res = await fetch(endpoint, { method: "POST", headers, body, signal: AbortSignal.timeout(timeoutMs) });
    } catch (e) {
      const isTimeout = e?.name === "TimeoutError";
      const err = new JevError(redact(`${isTimeout ? "タイムアウト" : "接続エラー"}: ${e?.message ?? e}`),
        { code: isTimeout ? "timeout" : "network", where });
      if (!otherRetried) { otherRetried = true; continue; }
      throw err;
    }
    let text;
    try {
      text = await res.text();
    } catch (e) {
      const isTimeout = e?.name === "TimeoutError";
      const err = new JevError(redact(`${isTimeout ? "タイムアウト" : "接続エラー"}: ${e?.message ?? e}`),
        { code: isTimeout ? "timeout" : "network", where });
      if (!otherRetried) { otherRetried = true; continue; }
      throw err;
    }
    const status = res.status;
    if (status >= 200 && status < 300) {
      let data;
      try {
        data = JSON.parse(text);
      } catch {
        throw new JevError(`応答が JSON でない: ${redact(text).slice(0, 500)}`, { code: "bad-response", status, where });
      }
      const answers = data && typeof data === "object" ? data.answers : null;
      if (!answers || typeof answers !== "object" || Array.isArray(answers)) {
        throw new JevError(`応答に answers が無い: ${redact(text).slice(0, 500)}`, { code: "bad-response", status, where });
      }
      return data;
    }
    const err = new JevError(`HTTP ${status}: ${redact(text).slice(0, 500)}`, { code: "http", status, where });
    if (status === 429 || status === 529) {
      if (rateRetries < maxRetries) {
        await sleep(retryWaitMs(res.headers.get("retry-after"), rateRetries));
        rateRetries += 1;
        continue;
      }
      throw err;
    }
    if (status >= 500 && status <= 599 && !otherRetried) { otherRetried = true; continue; }
    throw err;
  }
}
