import assert from "node:assert/strict";
import test from "node:test";
import { EventHub } from "../src/eventHub.js";

test("publishes events to subscribers and keeps bounded history", () => {
  const hub = new EventHub({ maxHistory: 2 });
  const received = [];
  const unsubscribe = hub.subscribe("default", (event) => received.push(event));

  hub.publish("default", { id: "1" });
  hub.publish("default", { id: "2" });
  hub.publish("default", { id: "3" });
  unsubscribe();
  hub.publish("default", { id: "4" });

  assert.deepEqual(received.map((event) => event.id), ["1", "2", "3"]);
  assert.deepEqual(hub.history("default").map((event) => event.id), ["3", "4"]);
});

test("replays history only when requested", () => {
  const hub = new EventHub({ maxHistory: 10 });
  hub.publish("default", { id: "1" });
  const received = [];
  hub.subscribe("default", (event) => received.push(event), { replay: true });
  assert.deepEqual(received.map((event) => event.id), ["1"]);
});
