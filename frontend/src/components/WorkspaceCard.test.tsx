import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { Workspace } from "../api/types";
import { WorkspaceCard } from "./WorkspaceCard";

const legacyWorkspace: Workspace = {
  id: "01234567-abcd-4000-8000-0123456789ab",
  name: "레거시 환경",
  profileId: "retired-python",
  profileVersion: 2,
  profileName: null,
  kernelName: null,
  kernelDisplayName: null,
  pythonVersion: null,
  cpuLimit: null,
  memoryLimitMb: null,
  privateDiskLimitMb: null,
  privateDiskQuotaEnforced: null,
  desiredState: "STOPPED",
  observedState: "STOPPED",
  progressPercent: null,
  stale: false,
  lastErrorCode: null,
  lastErrorSummary: null,
  createdAt: null,
  updatedAt: null,
  owner: null,
  restartRequired: false,
  deletionCheckpoint: null,
  deletionStatus: null,
  canRetryDelete: false,
  activeOperation: null,
};
const noop = async () => {};

function render(workspace: Workspace): string {
  return renderToStaticMarkup(<WorkspaceCard
    workspace={workspace}
    profile={null}
    operation={null}
    busy={false}
    launchUrl="/launch"
    onStart={noop}
    onStop={noop}
    onRestart={noop}
    onDelete={noop}
    onEnvironmentChanged={noop}
  />);
}

describe("WorkspaceCard profile compatibility", () => {
  it("keeps a legacy workspace readable when its profile left the catalog", () => {
    const html = render(legacyWorkspace);
    expect(html).toContain("retired-python");
    expect(html).toContain("v2");
    expect(html).toContain("01234567");
  });

  it("uses optional pinned profile details without requiring them", () => {
    const html = render({
      ...legacyWorkspace,
      profileName: "Pinned Python",
      kernelDisplayName: "Python 3 (ipykernel)",
      pythonVersion: "3.11.9",
      cpuLimit: "2.0",
      memoryLimitMb: 2048,
      privateDiskLimitMb: 1024,
      privateDiskQuotaEnforced: false,
    });
    expect(html).toContain("Pinned Python");
    expect(html).toContain("Python 3 (ipykernel)");
    expect(html).toContain("Python 3.11.9");
    expect(html).toContain("메모리 2 GB");
    expect(html).toContain("저장공간 개별 하드 제한 없음 (호스트 가용량까지)");
    expect(html).toContain("호스트 저장공간은 모든 개발환경이 함께 사용합니다.");
    expect(html).not.toContain("저장공간 정책값 1 GB");
  });

  it("labels an enforced pinned quota as a hard limit", () => {
    const html = render({
      ...legacyWorkspace,
      privateDiskLimitMb: 1024,
      privateDiskQuotaEnforced: true,
    });
    expect(html).toContain("저장공간 하드 제한 1 GB");
    expect(html).not.toContain("하드 제한 미적용");
  });

  it("does not silently hide a malformed enforced quota", () => {
    const html = render({
      ...legacyWorkspace,
      privateDiskLimitMb: null,
      privateDiskQuotaEnforced: true,
    });
    expect(html).toContain("저장공간 제한 정보 확인 필요");
  });

  it("locks deletion while pending and offers an explicit retry after failure", () => {
    const pending = render({
      ...legacyWorkspace,
      desiredState: "DELETED",
      deletionStatus: "RUNNING",
      deletionCheckpoint: "DELETION_PENDING",
    });
    expect(pending).toContain("개인 환경 삭제 진행 중");
    expect(pending).toMatch(/<button[^>]*disabled=""[^>]*>삭제/);

    const failed = render({
      ...legacyWorkspace,
      desiredState: "DELETED",
      observedState: "FAILED",
      deletionStatus: "FAILED",
      deletionCheckpoint: "DELETION_FAILED",
      canRetryDelete: true,
      lastErrorCode: "DELETE_FAILED",
    });
    expect(failed).toContain("삭제 재시도");
    expect(failed).not.toMatch(/<button[^>]*disabled=""[^>]*>삭제 재시도/);
  });

  it("surfaces the explicit restart action for running environment changes", () => {
    const html = render({
      ...legacyWorkspace,
      desiredState: "RUNNING",
      observedState: "RUNNING",
      restartRequired: true,
    });
    expect(html).toContain("재시작 필요");
    expect(html).toContain("변경사항 적용 (재시작)");
  });

  it.each(["NOT_FOUND", "STOPPED", "FAILED"] as const)(
    "offers an enabled stop action for stale RUNNING intent observed as %s",
    (observedState) => {
      const html = render({
        ...legacyWorkspace,
        desiredState: "RUNNING",
        observedState,
        stale: true,
      });
      expect(html).toContain(
        '<button class="button button--secondary" type="button">중지</button>',
      );
      expect(html).not.toContain(
        '<button class="button button--secondary" type="button" disabled="">시작</button>',
      );
    },
  );
});
