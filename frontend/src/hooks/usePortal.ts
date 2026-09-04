import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, portalApi } from "../api/client";
import type {
  AdminAuditEvent,
  Capacity,
  Operation,
  PortalUser,
  Workspace,
  WorkspaceMutationResult,
  WorkspaceProfile,
} from "../api/types";
import { isAdminUser } from "../lib/access";
import { presentError } from "../lib/display";
import { selectLatestOperationsByWorkspace } from "../lib/operations";
import { markWorkspaceUsageStale } from "../lib/resourceUsage";

type SessionState = "CHECKING" | "AUTHENTICATED" | "ANONYMOUS";

const activeOperationStatuses = new Set(["PENDING", "RUNNING", "WAITING_EXTERNAL"]);
const fastWorkspaceStates = new Set(["STARTING", "STOPPING"]);

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function isUnauthorized(error: unknown): boolean {
  return error instanceof ApiError && error.status === 401;
}

function replaceWorkspace(current: Workspace[], incoming: Workspace): Workspace[] {
  const index = current.findIndex((workspace) => workspace.id === incoming.id);
  if (index < 0) return [incoming, ...current];
  const next = [...current];
  next[index] = incoming;
  return next;
}

function settle<T>(promise: Promise<T>): Promise<PromiseSettledResult<T>> {
  return promise.then(
    (value) => ({ status: "fulfilled", value }),
    (reason: unknown) => ({ status: "rejected", reason }),
  );
}

export function usePortal() {
  const [session, setSession] = useState<SessionState>("CHECKING");
  const [sessionExpired, setSessionExpired] = useState(false);
  const [user, setUser] = useState<PortalUser | null>(null);
  const [profiles, setProfiles] = useState<WorkspaceProfile[]>([]);
  const [capacity, setCapacity] = useState<Capacity | null>(null);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [operations, setOperations] = useState<Record<string, Operation>>({});
  const [auditEvents, setAuditEvents] = useState<AdminAuditEvent[]>([]);
  const [auditWarning, setAuditWarning] = useState<string | null>(null);
  const [profilesError, setProfilesError] = useState<string | null>(null);
  const [capacityError, setCapacityError] = useState<string | null>(null);
  const [profilesLoading, setProfilesLoading] = useState(false);
  const [initialLoading, setInitialLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [creating, setCreating] = useState(false);
  const [provisioningRequesting, setProvisioningRequesting] = useState(false);
  const [busyWorkspaceIds, setBusyWorkspaceIds] = useState<Set<string>>(new Set());
  const [notice, setNotice] = useState<string | null>(null);
  const [syncWarning, setSyncWarning] = useState<string | null>(null);
  const [delegatedAuthRequired, setDelegatedAuthRequired] = useState(false);
  const [lastUpdatedAt, setLastUpdatedAt] = useState<Date | null>(null);
  const refreshInFlight = useRef(false);
  const provisioningRequestInFlight = useRef(false);

  const expireSession = useCallback(() => {
    setSessionExpired(true);
    setSession("ANONYMOUS");
    setUser(null);
    setAuditEvents([]);
    setAuditWarning(null);
  }, []);

  const showRequestError = useCallback((error: unknown) => {
    if (isUnauthorized(error)) {
      expireSession();
      return;
    }
    setNotice(presentError(error));
  }, [expireSession]);

  useEffect(() => {
    const controller = new AbortController();

    async function loadInitial() {
      try {
        const currentUser = await portalApi.me(controller.signal);
        if (controller.signal.aborted) return;
        setUser(currentUser);
        setSession("AUTHENTICATED");

        if (currentUser.status === "DISABLED") return;

        const auditRequest = isAdminUser(currentUser)
          ? settle(portalApi.adminAuditEvents(controller.signal))
          : Promise.resolve(null);
        const [results, auditResult] = await Promise.all([
          Promise.allSettled([
            portalApi.profiles(controller.signal),
            portalApi.capacity(controller.signal),
            portalApi.workspaces(controller.signal),
          ]),
          auditRequest,
        ]);
        if (controller.signal.aborted) return;

        const [profileResult, capacityResult, workspaceResult] = results;
        if (profileResult.status === "fulfilled") {
          setProfiles(profileResult.value);
          setProfilesError(null);
        } else {
          setProfiles([]);
          setProfilesError(presentError(profileResult.reason));
        }
        if (capacityResult.status === "fulfilled") {
          setCapacity(capacityResult.value);
          setCapacityError(null);
        } else {
          setCapacity(null);
          setCapacityError(presentError(capacityResult.reason));
        }
        if (workspaceResult.status === "fulfilled") setWorkspaces(workspaceResult.value);

        if (auditResult?.status === "fulfilled") {
          setAuditEvents(auditResult.value);
          setAuditWarning(null);
        } else if (auditResult?.status === "rejected") {
          if (isUnauthorized(auditResult.reason)) {
            expireSession();
            return;
          }
          setAuditWarning(presentError(auditResult.reason));
        }

        const rejected = results.find((result) => result.status === "rejected");
        if (rejected?.status === "rejected") {
          if (isUnauthorized(rejected.reason)) expireSession();
          else setNotice(presentError(rejected.reason));
        } else {
          setLastUpdatedAt(new Date());
        }
      } catch (error) {
        if (isAbort(error)) return;
        if (isUnauthorized(error)) {
          setSession("ANONYMOUS");
        } else {
          setSession("ANONYMOUS");
          setNotice(presentError(error));
        }
      } finally {
        if (!controller.signal.aborted) setInitialLoading(false);
      }
    }

    void loadInitial();
    return () => controller.abort();
  }, [expireSession]);

  const activeOperationIds = useMemo(
    () => [...new Set([
      ...Object.values(operations)
        .filter((operation) => activeOperationStatuses.has(operation.status))
        .map((operation) => operation.id),
      ...workspaces
        .map((workspace) => workspace.activeOperation)
        .filter((operation): operation is Operation => Boolean(
          operation && activeOperationStatuses.has(operation.status),
        ))
        .map((operation) => operation.id),
    ])],
    [operations, workspaces],
  );

  const refreshRuntime = useCallback(async (interactive = false) => {
    if (refreshInFlight.current || session !== "AUTHENTICATED" || user?.status === "DISABLED") {
      return;
    }
    refreshInFlight.current = true;
    if (interactive) {
      setRefreshing(true);
      setProfilesLoading(true);
    }

    try {
      const auditRequest = isAdminUser(user)
        ? settle(portalApi.adminAuditEvents())
        : Promise.resolve(null);
      const [results, auditResult] = await Promise.all([
        Promise.allSettled([
          portalApi.me(),
          interactive ? portalApi.profiles() : Promise.resolve(null),
          portalApi.workspaces(),
          portalApi.capacity(),
          ...activeOperationIds.map((operationId) => portalApi.operation(operationId)),
        ]),
        auditRequest,
      ]);
      const [
        userResult,
        profileResult,
        workspaceResult,
        capacityResult,
        ...operationResults
      ] = results;
      const unauthorized = [...results, ...(auditResult ? [auditResult] : [])].find(
        (result) => result.status === "rejected" && isUnauthorized(result.reason),
      );
      if (unauthorized) {
        expireSession();
        return;
      }

      const refreshedUser = userResult.status === "fulfilled" ? userResult.value : user;
      if (userResult.status === "fulfilled") setUser(userResult.value);
      if (interactive) {
        if (profileResult.status === "fulfilled" && profileResult.value !== null) {
          setProfiles(profileResult.value);
          setProfilesError(null);
        } else if (profileResult.status === "rejected") {
          setProfiles([]);
          setProfilesError(presentError(profileResult.reason));
        }
      }
      if (workspaceResult.status === "fulfilled") {
        setWorkspaces(workspaceResult.value);
      } else {
        setWorkspaces((current) => markWorkspaceUsageStale(current));
      }
      if (capacityResult.status === "fulfilled") {
        setCapacity(capacityResult.value);
        setCapacityError(null);
      } else {
        setCapacity(null);
        setCapacityError(presentError(capacityResult.reason));
      }

      if (!isAdminUser(refreshedUser)) {
        setAuditEvents([]);
        setAuditWarning(null);
      } else if (auditResult?.status === "fulfilled") {
        setAuditEvents(auditResult.value);
        setAuditWarning(null);
      } else if (auditResult?.status === "rejected") {
        setAuditWarning(presentError(auditResult.reason));
      }

      const refreshedOperations = operationResults
        .filter((result): result is PromiseFulfilledResult<Operation> => result.status === "fulfilled")
        .map((result) => result.value);
      if (refreshedOperations.length > 0) {
        setOperations((current) => {
          const next = { ...current };
          for (const operation of refreshedOperations) next[operation.id] = operation;
          return next;
        });
        if (refreshedOperations.some((operation) => operation.status === "AUTH_REQUIRED")) {
          setDelegatedAuthRequired(true);
        }
      }

      const rejected = results.find((result) => result.status === "rejected");
      if (rejected?.status === "rejected") {
        setSyncWarning(presentError(rejected.reason));
      } else {
        setSyncWarning(null);
        setLastUpdatedAt(new Date());
      }
    } finally {
      refreshInFlight.current = false;
      if (interactive) {
        setRefreshing(false);
        setProfilesLoading(false);
      }
    }
  }, [activeOperationIds, expireSession, session, user]);

  const needsFastPolling = useMemo(
    () =>
      activeOperationIds.length > 0 ||
      workspaces.some((workspace) => fastWorkspaceStates.has(workspace.observedState)) ||
      workspaces.some((workspace) => workspace.desiredState === "DELETED" &&
        ["PENDING", "RUNNING"].includes(workspace.deletionStatus ?? "")) ||
      (user?.status === "PROVISIONING" &&
        ["PENDING", "RUNNING"].includes(user.provisioning.status)),
    [activeOperationIds.length, user?.provisioning.status, user?.status, workspaces],
  );

  useEffect(() => {
    if (session !== "AUTHENTICATED" || user?.status === "DISABLED") return;
    const interval = window.setInterval(
      () => void refreshRuntime(false),
      needsFastPolling ? 2_000 : 15_000,
    );
    return () => window.clearInterval(interval);
  }, [needsFastPolling, refreshRuntime, session, user?.status]);

  const trackMutation = useCallback((result: WorkspaceMutationResult) => {
    const operation = {
      ...result.operation,
      workspaceId: result.operation.workspaceId || result.workspace.id,
    };
    setWorkspaces((current) => replaceWorkspace(current, result.workspace));
    setOperations((current) => ({ ...current, [operation.id]: operation }));
    setSyncWarning(null);
  }, []);

  const requestProvisioning = useCallback(async () => {
    if (provisioningRequestInFlight.current || user?.status !== "PROVISIONING") return;
    provisioningRequestInFlight.current = true;
    setProvisioningRequesting(true);
    setNotice(null);
    try {
      const provisioning = await portalApi.requestProvisioning();
      setUser((current) => current ? { ...current, provisioning } : current);
      setNotice(
        provisioning.status === "FAILED"
          ? "개인 개발공간 준비를 다시 요청했습니다."
          : "개인 개발공간 준비를 요청했습니다.",
      );
      await refreshRuntime(false);
    } catch (error) {
      showRequestError(error);
    } finally {
      provisioningRequestInFlight.current = false;
      setProvisioningRequesting(false);
    }
  }, [refreshRuntime, showRequestError, user?.status]);

  const createWorkspace = useCallback(async (
    profileId: string,
    profileVersion: number,
    name?: string,
  ) => {
    setCreating(true);
    setNotice(null);
    try {
      const result = await portalApi.createWorkspace(profileId, profileVersion, {
        name,
      });
      trackMutation(result);
      setNotice("개발환경을 만들었습니다. 환경변수를 설정한 뒤 시작할 수 있습니다.");
      await refreshRuntime(false);
      return true;
    } catch (error) {
      showRequestError(error);
      if (error instanceof ApiError && error.status === 429) {
        await refreshRuntime(false);
      }
      return false;
    } finally {
      setCreating(false);
    }
  }, [refreshRuntime, showRequestError, trackMutation]);

  const runWorkspaceAction = useCallback(async (
    workspaceId: string,
    action: "start" | "stop" | "restart" | "delete",
  ) => {
    setBusyWorkspaceIds((current) => new Set(current).add(workspaceId));
    setNotice(null);
    try {
      const result = action === "start"
        ? await portalApi.startWorkspace(workspaceId)
        : action === "stop"
          ? await portalApi.stopWorkspace(workspaceId)
          : action === "restart"
            ? await portalApi.restartWorkspace(workspaceId)
            : await portalApi.deleteWorkspace(workspaceId);
      trackMutation(result);
      setNotice(
        action === "start"
          ? "환경 시작을 요청했습니다."
          : action === "stop"
            ? "환경 중지를 요청했습니다."
            : action === "restart"
              ? "환경변수 변경사항을 적용하도록 재시작을 요청했습니다."
              : "환경 삭제를 요청했습니다. 개인 데이터 삭제가 완료될 때까지 상태를 갱신합니다.",
      );
    } catch (error) {
      showRequestError(error);
    } finally {
      setBusyWorkspaceIds((current) => {
        const next = new Set(current);
        next.delete(workspaceId);
        return next;
      });
    }
  }, [showRequestError, trackMutation]);

  const logout = useCallback(async () => {
    setNotice(null);
    try {
      const hubLogoutUrl = await portalApi.logout();
      setSessionExpired(false);
      setSession("ANONYMOUS");
      setUser(null);
      setOperations({});
      setAuditEvents([]);
      setAuditWarning(null);
      window.location.assign(hubLogoutUrl);
    } catch (error) {
      if (isUnauthorized(error)) expireSession();
      else setNotice(presentError(error));
    }
  }, [expireSession]);

  const latestOperationByWorkspace = useMemo(
    () => selectLatestOperationsByWorkspace(workspaces, operations),
    [operations, workspaces],
  );

  return {
    session,
    sessionExpired,
    user,
    profiles,
    profilesLoading,
    profilesError,
    capacity,
    capacityError,
    workspaces,
    auditEvents,
    auditWarning,
    latestOperationByWorkspace,
    initialLoading,
    refreshing,
    creating,
    provisioningRequesting,
    busyWorkspaceIds,
    notice,
    syncWarning,
    delegatedAuthRequired,
    lastUpdatedAt,
    dismissNotice: () => setNotice(null),
    requestProvisioning,
    createWorkspace,
    startWorkspace: (workspaceId: string) => runWorkspaceAction(workspaceId, "start"),
    stopWorkspace: (workspaceId: string) => runWorkspaceAction(workspaceId, "stop"),
    restartWorkspace: (workspaceId: string) => runWorkspaceAction(workspaceId, "restart"),
    deleteWorkspace: (workspaceId: string) => runWorkspaceAction(workspaceId, "delete"),
    refresh: () => refreshRuntime(true),
    logout,
  };
}
