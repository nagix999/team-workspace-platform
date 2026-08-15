import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { EnvironmentVariable } from "../api/types";
import {
  canReplaceEnvironmentVariable,
  EnvironmentRestartDialog,
  EnvironmentVariablesPanel,
} from "./EnvironmentVariablesPanel";

const variable = (isSecret: boolean, value: string | null): EnvironmentVariable => ({
  name: "TEAM_TOKEN",
  scope: "USER",
  version: 1,
  isSecret,
  value,
  isSet: true,
  updatedAt: null,
});

describe("EnvironmentVariablesPanel", () => {
  it("distinguishes visible plain values from write-only secret values", () => {
    const html = renderToStaticMarkup(<EnvironmentVariablesPanel />);
    expect(html).toContain("모든 내 환경의 환경변수");
    expect(html).toContain('<option value="secret" selected="">비밀 값</option>');
    expect(html).toContain('<option value="plain">일반 값</option>');
    expect(html).toContain('type="password"');
    expect(html).toContain("비밀 값은 저장 후 다시 표시되지 않으며");
    expect(html).toContain("일반 값은 소유자 화면에서 조회·편집할 수 있습니다");
  });

  it("labels a workspace-scoped override separately", () => {
    const html = renderToStaticMarkup(
      <EnvironmentVariablesPanel workspaceId="workspace-1" workspaceState="RUNNING" />,
    );
    expect(html).toContain("환경별 환경변수");
    expect(html).toContain("사용자 공통 값을 이 환경에서 덮어씁니다");
  });

  it("requires an explicit non-empty replacement for a write-only secret", () => {
    const secret = variable(true, null);
    expect(canReplaceEnvironmentVariable(secret, undefined)).toBe(false);
    expect(canReplaceEnvironmentVariable(secret, "")).toBe(false);
    expect(canReplaceEnvironmentVariable(secret, "new-secret")).toBe(true);

    const plain = variable(false, "visible");
    expect(canReplaceEnvironmentVariable(plain, undefined)).toBe(true);
  });

  it("shows a restart modal after changing a running workspace", () => {
    const html = renderToStaticMarkup(
      <EnvironmentRestartDialog
        workspaceId="workspace-1"
        workspaceState="RUNNING"
        action="saved"
        busy={false}
        onClose={() => undefined}
        onRestart={async () => undefined}
      />,
    );

    expect(html).toContain('role="dialog"');
    expect(html).toContain('aria-modal="true"');
    expect(html).toContain("환경변수 변경은 재실행 후 적용됩니다");
    expect(html).toContain("지금 다시 실행");
    expect(html).toContain("나중에");
  });

  it("warns that global changes require each running workspace to restart", () => {
    const html = renderToStaticMarkup(
      <EnvironmentRestartDialog
        action="deleted"
        busy={false}
        onClose={() => undefined}
      />,
    );

    expect(html).toContain("환경변수 삭제는 재실행 후 적용됩니다");
    expect(html).toContain("실행 중인 환경을 각각 다시 실행해야 합니다");
    expect(html).not.toContain("지금 다시 실행");
  });
});
