import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

vi.mock("./hooks/usePortal", () => ({
  usePortal: () => ({
    session: "AUTHENTICATED",
    sessionExpired: false,
    initialLoading: false,
    user: {
      id: "user-1",
      username: "alice",
      displayName: "Alice",
      role: "USER",
      status: "DISABLED",
      provisioning: {
        status: "NOT_REQUESTED",
        attempts: 0,
        errorCode: null,
        errorSummary: null,
        requestedAt: null,
        completedAt: null,
      },
    },
    profiles: [],
    lastUpdatedAt: null,
    delegatedAuthRequired: false,
    provisioningRequesting: false,
    notice: null,
    auditEvents: [],
    workspaces: [],
    busyWorkspaceIds: new Set<string>(),
    latestOperationByWorkspace: {},
    logout: vi.fn(),
    dismissNotice: vi.fn(),
    requestProvisioning: vi.fn(),
    refresh: vi.fn(),
  }),
}));

import App from "./App";

describe("authenticated account navigation", () => {
  it("links to the portal-owned password change redirect", () => {
    const html = renderToStaticMarkup(<App />);

    expect(html).toContain('href="/api/v1/auth/change-password"');
    expect(html).toContain("비밀번호 변경");
  });
});
