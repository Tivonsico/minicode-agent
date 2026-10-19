import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  casesIn,
  sourceSessions,
} from "../../../frontend/evaluation/catalog.js";

const tasks = JSON.parse(
  readFileSync(
    new URL("../../../backend/evaluation/data/tasks.json", import.meta.url),
  ),
).cases;
const planning = JSON.parse(
  readFileSync(
    new URL("../../../backend/evaluation/data/planning.json", import.meta.url),
  ),
).cases;
const imported = {
  id: "imported",
  mode: "history",
  source: { type: "session", session_id: "origin" },
};
const custom = { id: "custom", mode: "history", source: { type: "custom" } };
const cases = [...tasks, ...planning, imported, custom];

test("coding, planning and conversation rules have separate task populations", () => {
  assert.equal(casesIn(cases, "coding").length, 30);
  assert.equal(casesIn(cases, "planning").length, 4);
  assert.deepEqual(casesIn(cases, "history"), [imported, custom]);
});
test("category selection excludes hidden tasks and imported conversation rules", () => {
  assert.equal(casesIn(cases, "coding", "repair").length, 8);
  assert.equal(casesIn(cases, "coding", "security").length, 6);
  assert.deepEqual(casesIn(cases, "history", "imported"), [imported]);
  assert.deepEqual(casesIn(cases, "history", "custom"), [custom]);
  assert.deepEqual(casesIn(cases, "lite"), []);
  assert.deepEqual(casesIn(cases, "verified"), []);
});
test("imported rules inspect only their source, even outside the current session list", () => {
  assert.deepEqual(
    sourceSessions([imported, imported], [{ session_id: "other" }]),
    ["origin"],
  );
});
test("generic rules inspect eligible saved conversations without duplicating the importer", () => {
  assert.deepEqual(
    sourceSessions(
      [custom],
      [{ session_id: "one" }, { session_id: "bad", status: "error" }],
    ),
    ["one"],
  );
});
