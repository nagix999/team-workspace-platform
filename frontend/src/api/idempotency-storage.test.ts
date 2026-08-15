import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

class MemorySessionStorage implements Storage {
  private readonly values = new Map<string, string>();

  get length(): number {
    return this.values.size;
  }

  clear(): void {
    this.values.clear();
  }

  getItem(key: string): string | null {
    return this.values.get(key) ?? null;
  }

  key(index: number): string | null {
    return [...this.values.keys()][index] ?? null;
  }

  removeItem(key: string): void {
    this.values.delete(key);
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value);
  }

  snapshot(): string[] {
    return [...this.values.values()];
  }
}

const meResponse = {
  user: { id: "user-storage", username: "alice", status: "ACTIVE" },
  csrf_token: "csrf-token-stable-across-reload",
};

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("uncertain mutation persistence", () => {
  let storage: MemorySessionStorage;

  beforeEach(() => {
    storage = new MemorySessionStorage();
    Object.defineProperty(globalThis, "sessionStorage", {
      configurable: true,
      value: storage,
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    vi.resetModules();
    Reflect.deleteProperty(globalThis, "sessionStorage");
  });

  it("reuses an opaque persisted key after reload and clears it after confirmation", async () => {
    vi.resetModules();
    const firstClient = await import("./client");
    const firstFetch = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(jsonResponse(meResponse))
      .mockRejectedValueOnce(new TypeError("connection reset after commit"));

    await firstClient.portalApi.me();
    await expect(firstClient.portalApi.createWorkspace("offer-persisted", 1, {
      name: "재시도 환경",
    })).rejects.toMatchObject({ code: "NETWORK_ERROR" });

    const firstKey = new Headers(firstFetch.mock.calls[1]?.[1]?.headers)
      .get("Idempotency-Key");
    const stored = storage.snapshot().join("\n");
    expect(firstKey).toBeTruthy();
    expect(stored).not.toContain("재시도 환경");
    expect(JSON.parse(stored)[0]).toMatchObject({
      fingerprint: expect.stringMatching(/^[0-9a-f]{64}$/),
      key: firstKey,
      expiresAt: expect.any(Number),
    });
    firstFetch.mockRestore();

    vi.resetModules();
    const reloadedClient = await import("./client");
    const replay = {
      workspace: { id: "workspace-persisted", profile_id: "offer-persisted", profile_version: 1 },
      operation: {
        id: "operation-persisted",
        workspace_id: "workspace-persisted",
        operation_type: "CREATE",
        status: "PENDING",
      },
    };
    const secondFetch = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(jsonResponse(meResponse))
      .mockResolvedValueOnce(jsonResponse(replay, 202));

    await reloadedClient.portalApi.me();
    await reloadedClient.portalApi.createWorkspace("offer-persisted", 1, {
      name: "재시도 환경",
    });

    const secondKey = new Headers(secondFetch.mock.calls[1]?.[1]?.headers)
      .get("Idempotency-Key");
    expect(secondKey).toBe(firstKey);
    expect(storage.length).toBe(0);
  });

  it("clears unresolved entries after a confirmed logout", async () => {
    vi.resetModules();
    const client = await import("./client");
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(jsonResponse(meResponse))
      .mockRejectedValueOnce(new TypeError("connection reset after commit"))
      .mockResolvedValueOnce(jsonResponse({
        redirect_url: "https://hub.example.net/hub/logout",
      }));

    await client.portalApi.me();
    await expect(client.portalApi.startWorkspace("workspace-logout"))
      .rejects.toMatchObject({ code: "NETWORK_ERROR" });
    expect(storage.length).toBe(1);

    await expect(client.portalApi.logout())
      .resolves.toBe("https://hub.example.net/hub/logout");
    expect(fetchMock.mock.calls[2]?.[0]).toBe("/api/v1/auth/logout");
    expect(storage.length).toBe(0);
  });

  it("clears unresolved entries when the session is rejected", async () => {
    vi.resetModules();
    const client = await import("./client");
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(jsonResponse(meResponse))
      .mockRejectedValueOnce(new TypeError("connection reset after commit"))
      .mockResolvedValueOnce(jsonResponse({
        error: { code: "AUTH_REQUIRED", message: "session expired" },
      }, 401));

    await client.portalApi.me();
    await expect(client.portalApi.startWorkspace("workspace-expired"))
      .rejects.toMatchObject({ code: "NETWORK_ERROR" });
    expect(storage.length).toBe(1);

    await expect(client.portalApi.capacity())
      .rejects.toMatchObject({ status: 401, code: "AUTH_REQUIRED" });
    expect(fetchMock.mock.calls[2]?.[0]).toBe("/api/v1/capacity");
    expect(storage.length).toBe(0);
  });

  it("uses getRandomValues when randomUUID is unavailable", async () => {
    const platformCrypto = globalThis.crypto;
    vi.stubGlobal("crypto", {
      subtle: platformCrypto.subtle,
      getRandomValues: platformCrypto.getRandomValues.bind(platformCrypto),
    });
    vi.resetModules();
    const client = await import("./client");
    const response = {
      workspace: { id: "workspace-fallback", desired_state: "RUNNING" },
      operation: {
        id: "operation-fallback",
        workspace_id: "workspace-fallback",
        operation_type: "START",
        status: "PENDING",
      },
    };
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(jsonResponse(meResponse))
      .mockResolvedValueOnce(jsonResponse(response, 202));

    await client.portalApi.me();
    await client.portalApi.startWorkspace("workspace-fallback");

    expect(new Headers(fetchMock.mock.calls[1]?.[1]?.headers).get("Idempotency-Key"))
      .toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i);
  });
});
