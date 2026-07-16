import assert from "node:assert/strict";
import test from "node:test";
import { isAuthorized } from "../src/auth.js";

test("allows every request when token is not configured", () => {
  assert.equal(isAuthorized({ headers: {} }, ""), true);
});

test("requires exact bearer token when configured", () => {
  assert.equal(isAuthorized({ headers: { authorization: "Bearer secret" } }, "secret"), true);
  assert.equal(isAuthorized({ headers: { authorization: "secret" } }, "secret"), false);
  assert.equal(isAuthorized({ headers: { authorization: "Bearer other" } }, "secret"), false);
});
