import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

class NoopMutationObserver implements MutationObserver {
  disconnect() {}
  observe() {}
  takeRecords(): MutationRecord[] { return []; }
}

Object.defineProperty(globalThis, "MutationObserver", {
  configurable: true,
  value: NoopMutationObserver,
});

afterEach(async () => {
  cleanup();
  // Tabster batches Fluent UI focus-sentinel positioning on a 100 ms timer.
  await new Promise((resolve) => globalThis.setTimeout(resolve, 220));
});
