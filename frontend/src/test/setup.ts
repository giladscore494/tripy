import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

// jsdom has no layout: scrolling and object URLs are no-ops here
window.scrollTo = vi.fn() as unknown as typeof window.scrollTo;
URL.createObjectURL = vi.fn(() => "blob:mock");
URL.revokeObjectURL = vi.fn();

afterEach(() => {
  cleanup();
  window.sessionStorage.clear();
});
