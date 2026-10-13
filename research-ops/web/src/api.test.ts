import { afterEach, describe, expect, it, vi } from "vitest";
import { api, branchSocketUrl } from "./api";

afterEach(() => {
  sessionStorage.clear();
  vi.unstubAllGlobals();
});

describe("public workspace origin", () => {
  it.each([
    ["http:", "127.0.0.1:8020", "ws://127.0.0.1:8020"],
    ["https:", "research.example.com", "wss://research.example.com"],
  ])("uses the page origin for WebSockets on %s", (protocol, host, expected) => {
    vi.stubGlobal("window", { location: { protocol, host } });

    expect(branchSocketUrl("branch-1")).toBe(`${expected}/api/ws/branches/branch-1`);
  });

  it("preserves the optional access token on the HTTPS origin", () => {
    vi.stubGlobal("window", {
      location: { protocol: "https:", host: "research.example.com" },
    });
    sessionStorage.setItem("rops_access_token", "test token/+?");

    expect(branchSocketUrl("branch-1")).toBe(
      "wss://research.example.com/api/ws/branches/branch-1?token=test%20token%2F%2B%3F",
    );
  });

  it("keeps API requests relative to the browser origin", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => [] });
    vi.stubGlobal("fetch", fetchMock);

    await api.programs();

    expect(fetchMock).toHaveBeenCalledWith("/api/programs", expect.any(Object));
  });
});
