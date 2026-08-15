import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { AdminWorkspaceProfile, ResourcePolicy } from "../api/types";
import {
  AdminProfileManager,
  groupAdminProfilesByKernel,
} from "./AdminProfileManager";

const policy: ResourcePolicy = {
  version: 1,
  cpuBudgetMillicores: 8000,
  memoryBudgetMb: 8192,
  selectableCpuMillicores: [1000, 2000],
  selectableMemoryMb: [1024, 2048],
  availableCpuMillicores: [1000, 2000],
  availableMemoryMb: [1024, 2048],
  maxCpuBudgetMillicores: 16000,
  maxMemoryBudgetMb: 32768,
  updatedAt: null,
};

describe("AdminProfileManager", () => {
  it("offers only an approved runtime template and presentation metadata", () => {
    const html = renderToStaticMarkup(
      <AdminProfileManager resourcePolicy={policy} onProfilesChanged={async () => {}} />,
    );
    expect(html).toContain("검증된 Python 커널");
    expect(html).toContain("Python 커널 공개 설정");
    expect(html).toContain("모든 조합을 먼저 생성");
    expect(html).toContain("이미지와 실행 명령은 화면에서 입력할 수 없습니다");
    expect(html).not.toContain('type="file"');
    expect(html).not.toContain('name="image"');
    expect(html).not.toContain('name="command"');
  });

  it("groups every pre-created CPU and memory combination by Python kernel", () => {
    const profile = (
      id: string,
      kernelName: string,
      pythonVersion: string,
      cpuLimit: string,
      memoryLimitMb: number,
    ): AdminWorkspaceProfile => ({
      id,
      version: 1,
      name: id,
      description: null,
      enabled: true,
      effectiveSelectable: true,
      runtimeProfile: {
        id: `runtime-${id}`,
        version: 1,
        kernelName,
        kernelDisplayName: `Python ${pythonVersion}`,
        pythonVersion,
        cpuLimit,
        memoryLimitMb,
      },
      createdAt: null,
      updatedAt: null,
    });
    const groups = groupAdminProfilesByKernel([
      profile("py312-1-1", "python312", "3.12.13", "1.0", 1024),
      profile("py312-2-2", "python312", "3.12.13", "2.0", 2048),
      profile("py313-1-1", "python3", "3.13.14", "1.0", 1024),
    ]);

    expect(groups.map((group) => [group.key, group.profiles.length])).toEqual([
      ["python312:3.12.13", 2],
      ["python3:3.13.14", 1],
    ]);
  });
});
