// Tiny replacement for @testing-library/jest-dom's toHaveTextContent, kept dependency-free.
import { expect } from "bun:test";

expect.extend({
  toHaveTextContent(received: unknown, expected: string | RegExp) {
    const text = (received as HTMLElement)?.textContent ?? "";
    const pass = typeof expected === "string" ? text.includes(expected) : expected.test(text);
    return { pass, message: () => `expected element text ${JSON.stringify(text)} ${pass ? "not " : ""}to contain ${expected}` };
  },
});

declare module "bun:test" {
  interface Matchers<T> {
    toHaveTextContent(expected: string | RegExp): T;
  }
}
