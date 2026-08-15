import type { WorkspaceProfile } from "../api/types";

export type ProfileFacet =
  | "kernelName"
  | "pythonVersion"
  | "cpuLimit"
  | "memoryLimitMb";

export const profileFacetOrder: readonly ProfileFacet[] = [
  "kernelName",
  "pythonVersion",
  "cpuLimit",
  "memoryLimitMb",
];

export function profileKey(profile: WorkspaceProfile): string {
  return `${profile.id}:${profile.version}`;
}

export function cpuLimitToMillicores(value: string): number | null {
  const match = /^(0|[1-9]\d*)(?:\.(\d{1,3}))?$/.exec(value);
  if (!match) return null;
  const whole = Number(match[1]);
  const fraction = Number((match[2] ?? "").padEnd(3, "0"));
  const millicores = whole * 1000 + fraction;
  return Number.isSafeInteger(millicores) && millicores > 0 ? millicores : null;
}

export function profileFacetValue(
  profile: WorkspaceProfile,
  facet: ProfileFacet,
): string {
  return String(profile[facet]);
}

/**
 * Return only values that can still produce an allowlisted profile after all
 * earlier selectors. Later selectors intentionally do not constrain an
 * earlier choice: changing an earlier facet selects a valid downstream row.
 */
export function availableFacetValues(
  profiles: WorkspaceProfile[],
  selected: WorkspaceProfile,
  facet: ProfileFacet,
): string[] {
  const facetIndex = profileFacetOrder.indexOf(facet);
  const compatible = profiles.filter((profile) =>
    profileFacetOrder.slice(0, facetIndex).every(
      (earlier) => profileFacetValue(profile, earlier) ===
        profileFacetValue(selected, earlier),
    ));
  const values = [...new Set(
    compatible.map((profile) => profileFacetValue(profile, facet)),
  )];
  const numeric = facet === "cpuLimit" || facet === "memoryLimitMb";
  return values.sort((left, right) => numeric
    ? Number(left) - Number(right)
    : left.localeCompare(right, "ko-KR", { numeric: true }));
}

export function selectProfileForFacet(
  profiles: WorkspaceProfile[],
  selected: WorkspaceProfile,
  facet: ProfileFacet,
  value: string,
): WorkspaceProfile | null {
  const facetIndex = profileFacetOrder.indexOf(facet);
  return profiles.find((profile) =>
    profileFacetOrder.slice(0, facetIndex).every(
      (earlier) => profileFacetValue(profile, earlier) ===
        profileFacetValue(selected, earlier),
    ) && profileFacetValue(profile, facet) === value) ?? null;
}
