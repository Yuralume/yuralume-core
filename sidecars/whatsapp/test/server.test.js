import assert from "node:assert/strict";
import http from "node:http";
import test from "node:test";
import { EventHub } from "../src/eventHub.js";
import { createServer } from "../src/server.js";

test("message endpoint delegates to session manager", async () => {
  const eventHub = new EventHub();
  const sent = [];
  const server = createServer({
    config: { apiToken: "secret", sseHeartbeatMs: 1000 },
    eventHub,
    mediaStore: fakeMediaStore(),
    logger: fakeLogger(),
    sessionManager: {
      async start(sessionId) {
        return {
          async sendMessage(payload) {
            sent.push({ sessionId, ...payload });
            return ["message-1"];
          },
        };
      },
    },
  });
  await listen(server);
  const baseUrl = `http://127.0.0.1:${server.address().port}`;
  const response = await fetch(`${baseUrl}/sessions/default/messages`, {
    method: "POST",
    headers: {
      authorization: "Bearer secret",
      "content-type": "application/json",
    },
    body: JSON.stringify({ chat_ref: "user@s.whatsapp.net", text: "hello" }),
  });

  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { ok: true, message_ids: ["message-1"] });
  assert.deepEqual(sent, [{ sessionId: "default", chatRef: "user@s.whatsapp.net", text: "hello", attachments: [] }]);
  await close(server);
});

test("protected endpoints reject invalid bearer token", async () => {
  const server = createServer({
    config: { apiToken: "secret", sseHeartbeatMs: 1000 },
    eventHub: new EventHub(),
    mediaStore: fakeMediaStore(),
    logger: fakeLogger(),
    sessionManager: {},
  });
  await listen(server);
  const response = await fetch(`http://127.0.0.1:${server.address().port}/sessions/default/status`);
  assert.equal(response.status, 401);
  await close(server);
});

test("message endpoint rejects invalid json as a client error", async () => {
  const server = createServer({
    config: { apiToken: "", sseHeartbeatMs: 1000 },
    eventHub: new EventHub(),
    mediaStore: fakeMediaStore(),
    logger: fakeLogger(),
    sessionManager: {
      async start() {
        throw new Error("must not start session for invalid body");
      },
    },
  });
  await listen(server);

  const response = await fetch(`http://127.0.0.1:${server.address().port}/sessions/default/messages`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: "{not-json",
  });

  assert.equal(response.status, 400);
  assert.deepEqual(await response.json(), { ok: false, error: "request_body_invalid_json" });
  await close(server);
});

test("qr svg endpoint renders the current login qr", async () => {
  const server = createServer({
    config: { apiToken: "secret", sseHeartbeatMs: 1000 },
    eventHub: new EventHub(),
    mediaStore: fakeMediaStore(),
    logger: fakeLogger(),
    sessionManager: {
      async start(sessionId) {
        return {
          qr() {
            return { session_id: sessionId, qr: "whatsapp-login-qr", updated_at: "now" };
          },
        };
      },
    },
  });
  await listen(server);

  const response = await fetch(`http://127.0.0.1:${server.address().port}/sessions/default/qr.svg`, {
    headers: { authorization: "Bearer secret" },
  });
  const body = await response.text();

  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /image\/svg\+xml/);
  assert.match(body, /<svg/);
  await close(server);
});

function fakeMediaStore() {
  return {
    resolve() {
      return "missing";
    },
  };
}

function fakeLogger() {
  return {
    child: () => fakeLogger(),
    error: () => {},
    warn: () => {},
    info: () => {},
  };
}

function listen(server) {
  return new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
}

function close(server) {
  return new Promise((resolve) => server.close(resolve));
}
