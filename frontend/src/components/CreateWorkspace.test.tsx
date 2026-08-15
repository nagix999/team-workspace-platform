import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { Capacity, WorkspaceProfile } from "../api/types";
import {
  CreateWorkspace,
  resolveWorkspaceProfile,
  workspaceKernelKey,
} from "./CreateWorkspace";

const profile: WorkspaceProfile = {
  id: "python-standard",
  version: 7,
  name: "Python standard",
  description: "팀 표준 Python 환경",
  kernelName: "python3",
  kernelDisplayName: "Python 3 (ipykernel)",
  pythonVersion: "3.12.4",
  cpuLimit: "2.0",
  memoryLimitMb: 2048,
  privateDiskLimitMb: null,
  privateDiskQuotaEnforced: false,
  enabled: true,
};
const capacity: Capacity = {
  workspaceUsed: 1,
  workspaceLimit: 5,
  nextDefaultWorkspaceName: "환경-2",
  activeUsed: 3,
  activeLimit: 15,
  cpuReservedMillicores: 2000,
  cpuBudgetMillicores: 8000,
  memoryReservedMb: 1024,
  memoryBudgetMb: 4096,
  executionHostHealthy: true,
};
const noop = async () => {};
const createNoop = async () => true;

function render(overrides: Partial<Parameters<typeof CreateWorkspace>[0]> = {}): string {
  return renderToStaticMarkup(<CreateWorkspace
    profiles={[profile]}
    profilesLoading={false}
    profilesError={null}
    capacity={capacity}
    capacityError={null}
    userStatus="ACTIVE"
    creating={false}
    onRetry={noop}
    onCreate={createNoop}
    {...overrides}
  />);
}

describe("CreateWorkspace", () => {
  it("offers separate kernel, CPU and memory selectors backed by an exact profile", () => {
    const html = render();
    expect(html).not.toContain('id="profile-select"');
    expect(html).toContain("Python standard");
    expect(html).toContain('id="kernel-select"');
    expect(html).toContain('id="cpu-select"');
    expect(html).toContain('id="memory-select"');
    expect(html).toContain("Python 커널");
    expect(html).toContain("Memory");
    expect(html).toContain("기본 커널");
    expect(html).toContain("Python 3 (ipykernel)");
    expect(html).toContain("3.12.4");
    expect(html).not.toContain('for="disk-select"');
    expect(html).toContain("v7");
    expect(html).toContain("저장공간 개별 하드 제한 없음 (호스트 가용량까지)");
    expect(html).toContain("호스트 저장공간은 모든 개발환경이 함께 사용합니다.");
    expect(html).not.toContain("저장공간 정책값");
    expect(html).toContain('placeholder="환경-2"');
    expect(html).toContain("비워두면");
    expect(html).not.toContain("생성 시 환경변수 설정");
    expect(html).toContain("환경은 중지된 상태로 생성됩니다.");
  });

  it("resolves only the exact administrator-provided combination", () => {
    const memory4g = { ...profile, id: "python-standard-4g", memoryLimitMb: 4096 };
    expect(resolveWorkspaceProfile(
      [profile, memory4g],
      workspaceKernelKey(profile),
      "2.0",
      4096,
    )?.id).toBe("python-standard-4g");
    expect(resolveWorkspaceProfile(
      [profile, memory4g],
      "python312:3.12.4",
      "2.0",
      4096,
    )).toBeNull();

    expect(resolveWorkspaceProfile(
      [profile, { ...profile, id: "duplicate" }],
      workspaceKernelKey(profile),
      "2.0",
      2048,
    )).toBeNull();
  });

  it("labels storage as a hard limit only when enforcement is true", () => {
    const html = render({
      profiles: [{
        ...profile,
        privateDiskLimitMb: 1024,
        privateDiskQuotaEnforced: true,
      }],
    });
    expect(html).toContain("저장공간 하드 제한 1 GB");
    expect(html).not.toContain("하드 제한 미적용");
  });

  it("fails closed while capacity is unknown", () => {
    const html = render({ capacity: null });
    expect(html).toContain("용량 정보를 확인한 뒤 생성할 수 있습니다.");
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*>환경 생성/);
  });

  it("allows stopped creation even when runtime resources are currently full", () => {
    const cpuHtml = render({
      capacity: { ...capacity, cpuReservedMillicores: 7000 },
    });
    expect(cpuHtml).not.toContain("CPU 여유가 부족합니다.");
    expect(cpuHtml).not.toMatch(/<button[^>]*disabled=""[^>]*>환경 생성/);

    const memoryHtml = render({
      capacity: { ...capacity, memoryReservedMb: 3072 },
    });
    expect(memoryHtml).not.toContain("메모리 여유가 부족합니다.");
    expect(memoryHtml).not.toMatch(/<button[^>]*disabled=""[^>]*>환경 생성/);
  });

  it("does not require runtime resource totals until start", () => {
    const html = render({
      capacity: { ...capacity, cpuBudgetMillicores: null },
    });
    expect(html).not.toContain("CPU·메모리 가용량을 확인한 뒤 생성할 수 있습니다.");
    expect(html).not.toMatch(/<button[^>]*disabled=""[^>]*>환경 생성/);
  });

  it("exposes a retry action and no create path after a profile load error", () => {
    const html = render({
      profiles: [],
      profilesError: "프로필을 불러오지 못했습니다.",
    });
    expect(html).toContain('role="alert"');
    expect(html).toContain("프로필을 불러오지 못했습니다.");
    expect(html).toContain("설정 다시 불러오기");
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*>환경 생성/);
  });
});
