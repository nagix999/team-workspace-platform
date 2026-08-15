import { describe, expect, it, vi } from "vitest";
import {
  normalizeAuditEvents,
  normalizeAdminCapacity,
  normalizeAdminProfileCatalog,
  normalizeCapacity,
  normalizeEnvironmentList,
  normalizeOperation,
  normalizeProvisioning,
  normalizeProfiles,
  normalizeResourcePolicy,
  normalizeUser,
  normalizeWorkspace,
  portalApi,
} from "./client";

describe("portal API normalization", () => {
  it("normalizes the backend user and quota envelopes", () => {
    expect(normalizeUser({
      user: {
        id: "user-1",
        username: "alice",
        display_name: null,
        role: "ADMIN",
        status: "ACTIVE",
      },
    })).toMatchObject({ id: "user-1", username: "alice", role: "ADMIN", status: "ACTIVE" });

    expect(normalizeCapacity({
      user: { used: 2, limit: 5 },
      global: {
        active: 7,
        limit: 15,
        resources: {
          cpu_millicores: { reserved: 3500, limit: 8000 },
          memory_mb: { reserved: 3072, limit: 4096 },
        },
      },
      execution_host_healthy: true,
    })).toEqual({
      workspaceUsed: 2,
      workspaceLimit: 5,
      nextDefaultWorkspaceName: null,
      activeUsed: 7,
      activeLimit: 15,
      cpuReservedMillicores: 3500,
      cpuBudgetMillicores: 8000,
      memoryReservedMb: 3072,
      memoryBudgetMb: 4096,
      executionHostHealthy: true,
    });
  });

  it("normalizes incomplete capacity to a create-blocking state", () => {
    expect(normalizeCapacity({ execution_host_healthy: true })).toMatchObject({
      workspaceUsed: 0,
      workspaceLimit: 0,
      nextDefaultWorkspaceName: null,
      activeUsed: 0,
      activeLimit: 0,
      cpuReservedMillicores: null,
      cpuBudgetMillicores: null,
      memoryReservedMb: null,
      memoryBudgetMb: null,
      executionHostHealthy: true,
    });
    expect(normalizeCapacity({})).toMatchObject({
      workspaceLimit: 0,
      activeLimit: 0,
      executionHostHealthy: null,
    });
  });

  it("keeps admin created, running, and reserved counts distinct", () => {
    expect(normalizeAdminCapacity({
      users: 10,
      workspaces: { created: 23, running: 7, reserved: 9, limit: 15 },
      resources: {
        cpu_millicores: { reserved: 6500, limit: 12000 },
        memory_mb: { reserved: 7168, limit: 16384 },
      },
    })).toEqual({
      users: 10,
      workspaceCreated: 23,
      workspaceRunning: 7,
      workspaceReserved: 9,
      workspaceLimit: 15,
      cpuReservedMillicores: 6500,
      cpuBudgetMillicores: 12000,
      memoryReservedMb: 7168,
      memoryBudgetMb: 16384,
    });
  });

  it("normalizes resource choices and host hard ceilings", () => {
    expect(normalizeResourcePolicy({ resource_policy: {
      version: 4,
      cpu_budget_millicores: 12000,
      memory_budget_mb: 16384,
      selectable_cpu_millicores: [2000, 1000, 1000],
      selectable_memory_mb: [4096, 2048],
      available_cpu_millicores: [500, 1000, 2000, 4000],
      available_memory_mb: [1024, 2048, 4096, 8192],
      hard_ceiling: { cpu_millicores: 16000, memory_mb: 32768 },
      updated_at: "2026-08-11T01:00:00Z",
    } })).toMatchObject({
      version: 4,
      selectableCpuMillicores: [1000, 2000],
      selectableMemoryMb: [2048, 4096],
      maxCpuBudgetMillicores: 16000,
      maxMemoryBudgetMb: 32768,
    });
  });

  it("does not revive retired selections when the available catalog is explicitly empty", () => {
    expect(normalizeResourcePolicy({ resource_policy: {
      version: 5,
      cpu_budget_millicores: 12000,
      memory_budget_mb: 16384,
      selectable_cpu_millicores: [500],
      selectable_memory_mb: [1024],
      available_cpu_millicores: [],
      available_memory_mb: [],
    } })).toMatchObject({
      selectableCpuMillicores: [500],
      selectableMemoryMb: [1024],
      availableCpuMillicores: [],
      availableMemoryMb: [],
    });
  });

  it("never materializes a secret value but keeps a declared plain value editable", () => {
    const result = normalizeEnvironmentList({
      restart_required: true,
      items: [
        {
          name: "API_TOKEN",
          scope: "USER",
          version: 2,
          is_secret: true,
          value: "must-not-be-trusted",
          is_set: true,
        },
        {
          name: "TEAM_NAME",
          scope: "WORKSPACE",
          version: 1,
          is_secret: false,
          value: "ai-labs",
          is_set: true,
        },
      ],
    });
    expect(result.restartRequired).toBe(true);
    expect(result.items).toEqual([
      expect.objectContaining({ name: "API_TOKEN", isSecret: true, value: null }),
      expect.objectContaining({ name: "TEAM_NAME", isSecret: false, value: "ai-labs" }),
    ]);
    expect(JSON.stringify(result.items[0])).not.toContain("must-not-be-trusted");
  });

  it("normalizes admin offers without accepting image or command fields", () => {
    const catalog = normalizeAdminProfileCatalog({
      items: [{
        id: "offer-abc",
        version: 2,
        name: "Data science",
        description: "Approved",
        enabled: true,
        image: "must-not-render",
        command: ["must-not-render"],
        runtime_profile: {
          id: "python312-cpu2-mem2048",
          version: 1,
          kernel_name: "python312",
          kernel_display_name: "Python 3.12",
          python_version: "3.12.13",
          cpu_limit: "2.0",
          memory_limit_mb: 2048,
        },
      }],
      runtime_templates: [{
        id: "python312-cpu2-mem2048",
        version: 1,
        kernel_name: "python312",
        kernel_display_name: "Python 3.12",
        python_version: "3.12.13",
        cpu_limit: "2.0",
        memory_limit_mb: 2048,
      }],
    });
    expect(catalog.items).toHaveLength(1);
    expect(JSON.stringify(catalog)).not.toContain("must-not-render");
  });

  it("normalizes provisioning and safely falls back for older me responses", () => {
    expect(normalizeUser({
      user: { id: "active-user", username: "active", status: "ACTIVE" },
    }).provisioning).toMatchObject({ status: "SUCCEEDED", attempts: 0 });

    expect(normalizeUser({
      user: { id: "new-user", username: "new-user", status: "PROVISIONING" },
    }).provisioning).toMatchObject({ status: "MANUAL_REQUIRED", attempts: 0 });

    expect(normalizeProvisioning({
      provisioning: {
        status: "FAILED",
        attempts: 2,
        error_code: "PROVISIONING_FAILED",
        error_summary: "저장공간을 준비하지 못했습니다.",
        requested_at: "2026-08-10T01:00:00Z",
        completed_at: "2026-08-10T01:01:00Z",
      },
    })).toEqual({
      status: "FAILED",
      attempts: 2,
      errorCode: "PROVISIONING_FAILED",
      errorSummary: "저장공간을 준비하지 못했습니다.",
      requestedAt: "2026-08-10T01:00:00Z",
      completedAt: "2026-08-10T01:01:00Z",
    });

    expect(normalizeProvisioning({
      provisioning: { status: "UNRECOGNIZED", attempts: -4 },
    })).toMatchObject({ status: "MANUAL_REQUIRED", attempts: 0 });
  });

  it("normalizes profiles and workspace lifecycle values", () => {
    expect(normalizeProfiles({
      items: [{
        id: "python-local",
        version: 1,
        name: "Python local",
        kernel_name: "python3",
        kernel_display_name: "Python 3 (ipykernel)",
        python_version: "3.12.4",
        cpu_limit: "1.0",
        memory_limit_mb: 1024,
        private_disk_limit_mb: null,
        private_disk_quota_enforced: false,
      }],
    })).toEqual([expect.objectContaining({
      id: "python-local",
      version: 1,
      kernelName: "python3",
      kernelDisplayName: "Python 3 (ipykernel)",
      pythonVersion: "3.12.4",
      cpuLimit: "1.0",
      memoryLimitMb: 1024,
      privateDiskLimitMb: null,
      privateDiskQuotaEnforced: false,
    })]);

    expect(normalizeWorkspace({
      workspace: {
        id: "workspace-1",
        profile_id: "python-local",
        profile_version: 1,
        desired_state: "RUNNING",
        observed_state: "FAILED",
        private_disk_quota_enforced: false,
        stale: false,
        active_operation: {
          id: "operation-pending-stop",
          operation_type: "STOP",
          status: "PENDING",
          requested_at: "2026-08-11T01:02:03Z",
        },
      },
    })).toMatchObject({
      id: "workspace-1",
      observedState: "FAILED",
      privateDiskQuotaEnforced: false,
      activeOperation: {
        id: "operation-pending-stop",
        workspaceId: "workspace-1",
        operationType: "STOP",
        status: "PENDING",
      },
    });

    expect(normalizeOperation({
      operation: {
        id: "operation-1",
        workspace_id: "workspace-1",
        operation_type: "CREATE",
        status: "AUTH_REQUIRED",
      },
    })).toMatchObject({
      id: "operation-1",
      workspaceId: "workspace-1",
      status: "AUTH_REQUIRED",
    });
  });

  it("drops malformed, disabled, and duplicate profile rows fail-closed", () => {
    const valid = {
      id: "python-standard",
      version: 3,
      name: "Python standard",
      kernel_name: "python3",
      python_version: "3.12.4",
      cpu_limit: "2.0",
      memory_limit_mb: 2048,
      private_disk_limit_mb: 1024,
      private_disk_quota_enforced: true,
    };
    const profiles = normalizeProfiles({ items: [
      valid,
      { ...valid },
      { ...valid, id: "UPPERCASE" },
      { ...valid, id: "missing-kernel", kernel_name: null },
      { ...valid, id: "unsafe-kernel", kernel_name: "../../python" },
      { ...valid, id: "partial-python", python_version: "3.12" },
      { ...valid, id: "bad-version", version: 1.5 },
      { ...valid, id: "bad-cpu", cpu_limit: "unlimited" },
      { ...valid, id: "zero-memory", memory_limit_mb: 0 },
      { ...valid, id: "missing-hard-limit", private_disk_limit_mb: null },
      { ...valid, id: "missing-quota", private_disk_quota_enforced: undefined },
      { ...valid, id: "string-quota", private_disk_quota_enforced: "false" },
      { ...valid, id: "disabled", enabled: false },
    ] });

    expect(profiles).toHaveLength(1);
    expect(profiles[0]).toMatchObject({
      id: "python-standard",
      kernelDisplayName: "python3",
    });
  });

  it("treats an unenforced legacy disk value as host-capacity storage", () => {
    const base = {
      version: 1,
      name: "Python",
      kernel_name: "python3",
      python_version: "3.12.4",
      cpu_limit: "1.0",
      memory_limit_mb: 1024,
      private_disk_quota_enforced: false,
    };
    const profiles = normalizeProfiles({ items: [
      { ...base, id: "new-contract", private_disk_limit_mb: null },
      { ...base, id: "legacy-contract", private_disk_limit_mb: 1024 },
      { ...base, id: "missing-contract" },
      { ...base, id: "invalid-contract", private_disk_limit_mb: "unlimited" },
    ] });

    expect(profiles).toHaveLength(2);
    expect(profiles.every((profile) => profile.privateDiskLimitMb === null))
      .toBe(true);
  });

  it("drops every profile for a kernel with conflicting display labels", () => {
    const base = {
      version: 1,
      name: "Python",
      kernel_name: "python3",
      python_version: "3.12.4",
      cpu_limit: "1.0",
      memory_limit_mb: 1024,
      private_disk_limit_mb: 1024,
      private_disk_quota_enforced: true,
    };
    const profiles = normalizeProfiles({ items: [
      { ...base, id: "python-a", kernel_display_name: "Python 3" },
      { ...base, id: "python-b", kernel_display_name: "Unexpected label" },
      {
        ...base,
        id: "pyspark-a",
        kernel_name: "pyspark",
        kernel_display_name: "PySpark",
      },
    ] });

    expect(profiles.map((profile) => profile.id)).toEqual(["pyspark-a"]);
  });

  it("keeps only display-safe audit fields and discards metadata", () => {
    const events = normalizeAuditEvents({
      items: [{
        id: "audit-1",
        actor_user_id: "sensitive-actor-id",
        workspace_id: "workspace-1",
        action: "WORKSPACE_START_REQUESTED",
        result: "SUCCESS",
        request_id: "sensitive-request-id",
        safe_metadata_json: "{\"internal\":\"must-not-render\"}",
        created_at: "2026-08-10T01:02:03Z",
      }],
    });

    expect(events).toEqual([{
      id: "audit-1",
      workspaceId: "workspace-1",
      action: "WORKSPACE_START_REQUESTED",
      result: "SUCCESS",
      createdAt: "2026-08-10T01:02:03Z",
    }]);
    expect(JSON.stringify(events)).not.toContain("must-not-render");
    expect(JSON.stringify(events)).not.toContain("sensitive-request-id");
    expect(JSON.stringify(events)).not.toContain("sensitive-actor-id");
  });

  it("requests only the 20 most recent admin audit events by default", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(
      JSON.stringify({ items: [] }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    ));

    try {
      await expect(portalApi.adminAuditEvents()).resolves.toEqual([]);
      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/v1/admin/audit-events?limit=20&offset=0");
    } finally {
      fetchMock.mockRestore();
    }
  });

  it("requests personal provisioning with the me CSRF token", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(
        JSON.stringify({
          user: { id: "user-1", username: "alice", status: "PROVISIONING" },
          provisioning: null,
          csrf_token: "csrf-for-alice",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ))
      .mockResolvedValueOnce(new Response(
        JSON.stringify({
          provisioning: {
            status: "PENDING",
            attempts: 0,
            error_code: null,
            error_summary: null,
            requested_at: "2026-08-10T01:00:00Z",
            completed_at: null,
          },
        }),
        { status: 202, headers: { "Content-Type": "application/json" } },
      ));

    try {
      await portalApi.me();
      await expect(portalApi.requestProvisioning()).resolves.toMatchObject({ status: "PENDING" });

      expect(fetchMock).toHaveBeenCalledTimes(2);
      const [url, init] = fetchMock.mock.calls[1] ?? [];
      expect(url).toBe("/api/v1/me/provisioning");
      expect(init?.method).toBe("POST");
      expect(new Headers(init?.headers).get("X-CSRF-Token")).toBe("csrf-for-alice");
      expect(init?.credentials).toBe("same-origin");
    } finally {
      fetchMock.mockRestore();
    }
  });

  it("creates a workspace using only the selected allowlist tuple", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(
      JSON.stringify({
        workspace: {
          id: "workspace-1",
          profile_id: "python-standard-2cpu",
          profile_version: 4,
        },
        operation: {
          id: "operation-1",
          workspace_id: "workspace-1",
          operation_type: "CREATE",
          status: "PENDING",
        },
      }),
      { status: 202, headers: { "Content-Type": "application/json" } },
    ));

    try {
      await portalApi.createWorkspace("python-standard-2cpu", 4);
      const [url, init] = fetchMock.mock.calls[0] ?? [];
      expect(url).toBe("/api/v1/workspaces");
      expect(init?.method).toBe("POST");
      expect(JSON.parse(String(init?.body))).toEqual({
        profile_id: "python-standard-2cpu",
        profile_version: 4,
      });
      expect(JSON.parse(String(init?.body))).not.toHaveProperty("image");
      expect(JSON.parse(String(init?.body))).not.toHaveProperty("cpu_limit");
    } finally {
      fetchMock.mockRestore();
    }
  });

  it("creates a named workspace without a creation-time environment payload", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(
      JSON.stringify({
        workspace: { id: "workspace-2", name: "분석", profile_id: "offer-a", profile_version: 2 },
        operation: { id: "operation-2", workspace_id: "workspace-2", status: "PENDING" },
      }),
      { status: 202, headers: { "Content-Type": "application/json" } },
    ));
    try {
      await portalApi.createWorkspace("offer-a", 2, {
        name: " 분석 ",
      });
      const [, init] = fetchMock.mock.calls[0] ?? [];
      expect(JSON.parse(String(init?.body))).toEqual({
        profile_id: "offer-a",
        profile_version: 2,
        name: "분석",
      });
    } finally {
      fetchMock.mockRestore();
    }
  });

  it("reuses the idempotency key after an unknown network outcome", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockRejectedValueOnce(new TypeError("connection reset after commit"))
      .mockResolvedValueOnce(new Response(
        JSON.stringify({
          workspace: { id: "workspace-replayed", profile_id: "offer-retry", profile_version: 1 },
          operation: {
            id: "operation-replayed",
            workspace_id: "workspace-replayed",
            operation_type: "CREATE",
            status: "PENDING",
          },
        }),
        { status: 202, headers: { "Content-Type": "application/json" } },
      ));
    try {
      await expect(portalApi.createWorkspace("offer-retry", 1, { name: "재시도" }))
        .rejects.toMatchObject({ code: "NETWORK_ERROR" });
      await portalApi.createWorkspace("offer-retry", 1, { name: "재시도" });

      const firstKey = new Headers(fetchMock.mock.calls[0]?.[1]?.headers)
        .get("Idempotency-Key");
      const secondKey = new Headers(fetchMock.mock.calls[1]?.[1]?.headers)
        .get("Idempotency-Key");
      expect(firstKey).toBeTruthy();
      expect(secondKey).toBe(firstKey);
    } finally {
      fetchMock.mockRestore();
    }
  });

  it("keeps the idempotency key when a committed response body is truncated", async () => {
    const replayed = {
      workspace: { id: "workspace-truncated", profile_id: "offer-truncated", profile_version: 1 },
      operation: {
        id: "operation-truncated",
        workspace_id: "workspace-truncated",
        operation_type: "CREATE",
        status: "PENDING",
      },
    };
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response("{\"workspace\":", {
        status: 202,
        headers: { "Content-Type": "application/json" },
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify(replayed), {
        status: 202,
        headers: { "Content-Type": "application/json" },
      }));
    try {
      await expect(portalApi.createWorkspace("offer-truncated", 1))
        .rejects.toMatchObject({ code: "INVALID_RESPONSE" });
      await portalApi.createWorkspace("offer-truncated", 1);

      const firstKey = new Headers(fetchMock.mock.calls[0]?.[1]?.headers)
        .get("Idempotency-Key");
      const secondKey = new Headers(fetchMock.mock.calls[1]?.[1]?.headers)
        .get("Idempotency-Key");
      expect(firstKey).toBeTruthy();
      expect(secondKey).toBe(firstKey);
    } finally {
      fetchMock.mockRestore();
    }
  });

  it("keeps the idempotency key when a 2xx JSON body fails strict validation", async () => {
    const replayed = {
      workspace: { id: "workspace-invalid-shape", profile_id: "offer-shape", profile_version: 1 },
      operation: {
        id: "operation-invalid-shape",
        workspace_id: "workspace-invalid-shape",
        operation_type: "CREATE",
        status: "PENDING",
      },
    };
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(JSON.stringify({
        workspace: replayed.workspace,
        operation: { status: "PENDING" },
      }), {
        status: 202,
        headers: { "Content-Type": "application/json" },
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify(replayed), {
        status: 202,
        headers: { "Content-Type": "application/json" },
      }));
    try {
      await expect(portalApi.createWorkspace("offer-shape", 1))
        .rejects.toMatchObject({ code: "INVALID_RESPONSE" });
      await portalApi.createWorkspace("offer-shape", 1);

      const firstKey = new Headers(fetchMock.mock.calls[0]?.[1]?.headers)
        .get("Idempotency-Key");
      const secondKey = new Headers(fetchMock.mock.calls[1]?.[1]?.headers)
        .get("Idempotency-Key");
      expect(firstKey).toBeTruthy();
      expect(secondKey).toBe(firstKey);
    } finally {
      fetchMock.mockRestore();
    }
  });

  it("writes environment variables with type and optimistic version metadata", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(
      JSON.stringify({
        item: {
          name: "TEAM_NAME",
          scope: "WORKSPACE",
          version: 4,
          is_secret: false,
          value: "ai-labs",
          is_set: true,
        },
        changed: true,
        restart_required: true,
      }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    ));
    try {
      await portalApi.putEnvironmentVariable(
        "TEAM_NAME",
        "ai-labs",
        false,
        3,
        "workspace-1",
      );
      const [url, init] = fetchMock.mock.calls[0] ?? [];
      expect(url).toBe("/api/v1/workspaces/workspace-1/environment-variables/TEAM_NAME");
      expect(init?.method).toBe("PUT");
      expect(JSON.parse(String(init?.body))).toEqual({
        value: "ai-labs",
        is_secret: false,
        expected_version: 3,
      });
    } finally {
      fetchMock.mockRestore();
    }
  });

  it("accepts the backend deletion receipt without inventing a deleted item", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(
      JSON.stringify({
        deleted: true,
        name: "TEAM_NAME",
        scope: "WORKSPACE",
        version: 5,
        restart_required: true,
      }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    ));
    try {
      await expect(portalApi.deleteEnvironmentVariable(
        "TEAM_NAME",
        4,
        "workspace-1",
      )).resolves.toEqual({
        item: null,
        changed: true,
        restartRequired: true,
      });
    } finally {
      fetchMock.mockRestore();
    }
  });

  it("uses the explicit restart endpoint instead of overloading start", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(
      JSON.stringify({
        workspace: { id: "workspace-1", desired_state: "RUNNING" },
        operation: { id: "operation-3", workspace_id: "workspace-1", operation_type: "RESTART" },
      }),
      { status: 202, headers: { "Content-Type": "application/json" } },
    ));
    try {
      await portalApi.restartWorkspace("workspace-1");
      expect(fetchMock.mock.calls[0]?.[0]).toBe(
        "/api/v1/workspaces/workspace-1/actions/restart",
      );
      expect(fetchMock.mock.calls[0]?.[1]?.method).toBe("POST");
    } finally {
      fetchMock.mockRestore();
    }
  });

  it("loads every admin workspace page beyond the server's 100 item limit", async () => {
    const workspace = (index: number) => ({
      id: `workspace-${index}`,
      name: `Environment ${index}`,
      desired_state: "STOPPED",
      observed_state: "STOPPED",
    });
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      const offset = Number(new URL(url, "https://portal.invalid").searchParams.get("offset"));
      const count = offset === 0 ? 100 : 25;
      return new Response(JSON.stringify({
        items: Array.from({ length: count }, (_, index) => workspace(offset + index)),
        total: 125,
        limit: 100,
        offset,
      }), { status: 200, headers: { "Content-Type": "application/json" } });
    });
    try {
      const result = await portalApi.adminWorkspaces();
      expect(result.items).toHaveLength(125);
      expect(result.total).toBe(125);
      expect(result.items[124]?.id).toBe("workspace-124");
      expect(fetchMock).toHaveBeenCalledTimes(2);
    } finally {
      fetchMock.mockRestore();
    }
  });

  it.each([
    {
      label: "malformed total",
      response: { items: [], total: "125", limit: 100, offset: 0 },
    },
    {
      label: "a page that makes no valid progress",
      response: {
        items: [{ id: "workspace-1", desired_state: "STOPPED", observed_state: "STOPPED" }],
        total: 125,
        limit: 100,
        offset: 0,
      },
    },
    {
      label: "an inventory above the defensive team ceiling",
      response: { items: [], total: 10_001, limit: 100, offset: 0 },
    },
  ])("fails closed for $label in admin workspace inventory", async ({ response }) => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(
      JSON.stringify(response),
      { status: 200, headers: { "Content-Type": "application/json" } },
    ));
    try {
      await expect(portalApi.adminWorkspaces()).rejects.toMatchObject({
        code: "INVALID_RESPONSE",
      });
    } finally {
      fetchMock.mockRestore();
    }
  });

  it("fails closed when paged admin inventory changes total or repeats an id", async () => {
    const firstPage = Array.from({ length: 100 }, (_, index) => ({
      id: `workspace-${index}`,
      desired_state: "STOPPED",
      observed_state: "STOPPED",
    }));
    for (const invalidSecondPage of [
      { items: [{ id: "workspace-100" }], total: 102, limit: 100, offset: 100 },
      { items: [{ id: "workspace-0" }], total: 101, limit: 100, offset: 100 },
    ]) {
      let call = 0;
      const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async () => {
        const response = call++ === 0
          ? { items: firstPage, total: 101, limit: 100, offset: 0 }
          : invalidSecondPage;
        return new Response(JSON.stringify(response), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      });
      try {
        await expect(portalApi.adminWorkspaces()).rejects.toMatchObject({
          code: "INVALID_RESPONSE",
        });
      } finally {
        fetchMock.mockRestore();
      }
    }
  });

  it("strictly restores terminal admin operation history", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(
      JSON.stringify({
        items: [{
          id: "operation-terminal",
          workspace_id: "workspace-1",
          operation_type: "RESTART",
          status: "FAILED",
          error_code: "ADMIN_LIFECYCLE_UNAVAILABLE",
          error_summary: "관리자 실행 권한을 확인할 수 없습니다.",
          requested_at: "2026-08-12T01:00:00Z",
          completed_at: "2026-08-12T01:00:03Z",
        }],
        limit: 100,
        offset: 0,
      }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    ));
    try {
      await expect(portalApi.adminOperations()).resolves.toEqual([
        expect.objectContaining({
          id: "operation-terminal",
          workspaceId: "workspace-1",
          operationType: "RESTART",
          status: "FAILED",
          errorCode: "ADMIN_LIFECYCLE_UNAVAILABLE",
        }),
      ]);
    } finally {
      fetchMock.mockRestore();
    }
  });

  it("follows the exact admin operation page envelope beyond 100 records", async () => {
    const operationPayload = (index: number) => ({
      id: `operation-${index}`,
      workspace_id: `workspace-${index % 5}`,
      operation_type: "START",
      status: index === 100 ? "FAILED" : "SUCCEEDED",
      error_code: index === 100 ? "EXECUTION_HOST_UNHEALTHY" : null,
      error_summary: index === 100 ? "실행 호스트 상태를 확인할 수 없습니다." : null,
      requested_at: `2026-08-12T01:${String(Math.floor(index / 60)).padStart(2, "0")}:${String(index % 60).padStart(2, "0")}Z`,
      completed_at: "2026-08-12T02:00:00Z",
    });
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const offset = Number(
        new URL(String(input), "https://portal.invalid").searchParams.get("offset"),
      );
      const count = offset === 0 ? 100 : 1;
      return new Response(JSON.stringify({
        items: Array.from({ length: count }, (_, index) => operationPayload(offset + index)),
        limit: 100,
        offset,
      }), { status: 200, headers: { "Content-Type": "application/json" } });
    });
    try {
      const result = await portalApi.adminOperations();
      expect(result).toHaveLength(101);
      expect(result[100]).toMatchObject({
        id: "operation-100",
        status: "FAILED",
        errorCode: "EXECUTION_HOST_UNHEALTHY",
      });
      expect(fetchMock.mock.calls.map(([url]) => String(url))).toEqual([
        "/api/v1/admin/operations?limit=100&offset=0",
        "/api/v1/admin/operations?limit=100&offset=100",
      ]);
    } finally {
      fetchMock.mockRestore();
    }
  });

  it("fails closed when admin operation pages repeat an operation id", async () => {
    const operationPayload = (id: string) => ({
      id,
      workspace_id: "workspace-1",
      operation_type: "STOP",
      status: "SUCCEEDED",
      requested_at: "2026-08-12T01:00:00Z",
      completed_at: "2026-08-12T01:00:01Z",
    });
    let call = 0;
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async () => {
      const offset = call++ * 100;
      const items = offset === 0
        ? Array.from({ length: 100 }, (_, index) => operationPayload(`operation-${index}`))
        : [operationPayload("operation-0")];
      return new Response(JSON.stringify({ items, limit: 100, offset }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    });
    try {
      await expect(portalApi.adminOperations()).rejects.toMatchObject({
        code: "INVALID_RESPONSE",
      });
      expect(fetchMock).toHaveBeenCalledTimes(2);
    } finally {
      fetchMock.mockRestore();
    }
  });

  it("fails closed when full admin operation pages reach the defensive cap", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const offset = Number(
        new URL(String(input), "https://portal.invalid").searchParams.get("offset"),
      );
      const items = Array.from({ length: 100 }, (_, index) => ({
        id: `operation-${offset + index}`,
        workspace_id: `workspace-${(offset + index) % 5}`,
        operation_type: "START",
        status: "SUCCEEDED",
        requested_at: "2026-08-12T01:00:00Z",
        completed_at: "2026-08-12T01:00:01Z",
      }));
      return new Response(JSON.stringify({ items, limit: 100, offset }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    });
    try {
      await expect(portalApi.adminOperations()).rejects.toMatchObject({
        code: "INVALID_RESPONSE",
      });
      expect(fetchMock).toHaveBeenCalledTimes(100);
    } finally {
      fetchMock.mockRestore();
    }
  });
});

describe("logout redirect contract", () => {
  it("accepts only the exact HTTPS Hub logout path", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(JSON.stringify({
        redirect_url: "https://hub.example.net/hub/logout",
      }), { status: 200, headers: { "Content-Type": "application/json" } }));

    await expect(portalApi.logout()).resolves.toBe("https://hub.example.net/hub/logout");
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/v1/auth/logout");
  });

  it.each([
    "https://hub.example.net/hub/logout?next=https://attacker.test",
    "https://attacker@hub.example.net/hub/logout",
    "https://hub.example.net/other",
    "http://hub.example.net/hub/logout",
    "javascript:alert(1)",
  ])("rejects an unsafe logout redirect: %s", async (redirectUrl) => {
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(JSON.stringify({
        redirect_url: redirectUrl,
      }), { status: 200, headers: { "Content-Type": "application/json" } }));

    await expect(portalApi.logout()).rejects.toMatchObject({
      code: "INVALID_RESPONSE",
    });
  });
});
