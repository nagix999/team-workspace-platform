import type { UserProvisioning } from "../api/types";

interface ProvisioningBannerProps {
  provisioning: UserProvisioning;
  requesting: boolean;
  onRequest: () => Promise<void>;
}

function failureMessage(provisioning: UserProvisioning): string {
  return provisioning.errorSummary ??
    "저장공간을 준비하지 못했습니다. 잠시 후 다시 요청하거나 플랫폼 관리자에게 문의해 주세요.";
}

export function ProvisioningBanner({
  provisioning,
  requesting,
  onRequest,
}: ProvisioningBannerProps) {
  const inProgress = ["PENDING", "RUNNING"].includes(provisioning.status);

  if (provisioning.status === "MANUAL_REQUIRED") {
    return (
      <section className="alert alert--info" role="status">
        <span className="alert__icon alert__icon--pulse" aria-hidden="true" />
        <div>
          <strong>개인 개발공간을 준비하고 있습니다</strong>
          <p>관리자가 전용 저장공간 5개를 검증하는 중입니다. 완료되면 환경 생성 버튼이 자동으로 활성화됩니다.</p>
        </div>
      </section>
    );
  }

  if (provisioning.status === "FAILED") {
    return (
      <section className="alert alert--warning" role="alert">
        <span className="alert__icon" aria-hidden="true">!</span>
        <div>
          <strong>개인 개발공간을 준비하지 못했습니다</strong>
          <p>{failureMessage(provisioning)}</p>
        </div>
        <button
          className="button button--compact"
          type="button"
          disabled={requesting}
          onClick={() => void onRequest()}
        >
          {requesting ? (
            <><span className="spinner" aria-hidden="true" /> 요청하는 중</>
          ) : "다시 준비 요청"}
        </button>
      </section>
    );
  }

  if (provisioning.status === "NOT_REQUESTED") {
    return (
      <section className="alert alert--info" aria-labelledby="provisioning-heading">
        <span className="alert__icon" aria-hidden="true">→</span>
        <div>
          <strong id="provisioning-heading">개인 개발공간을 준비해 주세요</strong>
          <p>웹에서 준비를 요청하면 격리된 전용 저장공간 5개를 자동으로 생성합니다.</p>
        </div>
        <button
          className="button button--compact"
          type="button"
          disabled={requesting}
          onClick={() => void onRequest()}
        >
          {requesting ? (
            <><span className="spinner" aria-hidden="true" /> 요청하는 중</>
          ) : "개인 개발공간 준비"}
        </button>
      </section>
    );
  }

  if (inProgress) {
    return (
      <section className="alert alert--info" role="status" aria-live="polite" aria-busy="true">
        <span className="alert__icon alert__icon--pulse" aria-hidden="true" />
        <div>
          <strong>
            {provisioning.status === "PENDING"
              ? "개인 개발공간 준비 요청을 접수했습니다"
              : "개인 개발공간을 준비하고 있습니다"}
          </strong>
          <p>전용 저장공간을 검증하고 있습니다. 완료되면 환경 생성 버튼이 자동으로 활성화됩니다.</p>
        </div>
      </section>
    );
  }

  return (
    <section className="alert alert--info" role="status" aria-live="polite" aria-busy="true">
      <span className="alert__icon alert__icon--pulse" aria-hidden="true" />
      <div>
        <strong>개인 개발공간 준비를 완료했습니다</strong>
        <p>계정 상태를 반영하고 있습니다. 잠시만 기다려 주세요.</p>
      </div>
    </section>
  );
}
