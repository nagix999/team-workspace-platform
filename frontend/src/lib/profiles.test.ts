import { describe, expect, it } from "vitest";
import type { WorkspaceProfile } from "../api/types";
import {
  availableFacetValues,
  cpuLimitToMillicores,
  selectProfileForFacet,
} from "./profiles";

const profiles: WorkspaceProfile[] = [
  profile("python-311-small", "python3", "3.11.9", "1.0", 1024),
  profile("python-311-medium", "python3", "3.11.9", "2.0", 2048),
  profile("python-312-small", "python3", "3.12.4", "1.0", 1024),
  profile("pyspark-311", "pyspark", "3.11.9", "4.0", 4096),
];

function profile(
  id: string,
  kernelName: string,
  pythonVersion: string,
  cpuLimit: string,
  memoryLimitMb: number,
): WorkspaceProfile {
  return {
    id,
    version: 1,
    name: id,
    description: null,
    kernelName,
    kernelDisplayName: kernelName === "python3" ? "Python 3" : "PySpark",
    pythonVersion,
    cpuLimit,
    memoryLimitMb,
    privateDiskLimitMb: null,
    privateDiskQuotaEnforced: false,
    enabled: true,
  };
}

describe("profile facets", () => {
  it("converts only exact positive milllicore values", () => {
    expect(cpuLimitToMillicores("2")).toBe(2000);
    expect(cpuLimitToMillicores("0.125")).toBe(125);
    expect(cpuLimitToMillicores("0.0001")).toBeNull();
    expect(cpuLimitToMillicores("01")).toBeNull();
    expect(cpuLimitToMillicores("0")).toBeNull();
  });

  it("offers only combinations compatible with earlier selections", () => {
    expect(availableFacetValues(profiles, profiles[0], "kernelName"))
      .toEqual(["pyspark", "python3"]);
    expect(availableFacetValues(profiles, profiles[0], "pythonVersion"))
      .toEqual(["3.11.9", "3.12.4"]);
    expect(availableFacetValues(profiles, profiles[0], "cpuLimit"))
      .toEqual(["1.0", "2.0"]);
  });

  it("moves to an exact allowlisted row and resets downstream facets", () => {
    expect(selectProfileForFacet(profiles, profiles[0], "cpuLimit", "2.0")?.id)
      .toBe("python-311-medium");
    expect(selectProfileForFacet(profiles, profiles[0], "pythonVersion", "3.12.4")?.id)
      .toBe("python-312-small");
    expect(selectProfileForFacet(profiles, profiles[0], "cpuLimit", "99"))
      .toBeNull();
  });
});
