import type { PortalUser } from "../api/types";

export function isAdminUser(
  user: Pick<PortalUser, "role"> | null | undefined,
): boolean {
  return user?.role === "ADMIN";
}
