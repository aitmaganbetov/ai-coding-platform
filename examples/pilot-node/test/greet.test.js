import assert from "node:assert/strict";
import { test } from "node:test";

import { greet } from "../index.js";

test("greets by name", () => {
  assert.equal(greet("Ada"), "Hello, Ada!");
});

test("escapes HTML", () => {
  assert.equal(greet("<b>"), "Hello, &#60;b&#62;!");
});
