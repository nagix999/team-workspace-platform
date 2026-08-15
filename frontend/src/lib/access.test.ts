import { describe, expect, it } from "vitest";
import { isAdminUser } from "./access";

describe("portal role access", () => {
  it("permits audit access only for normalized ADMIN users", () => {
    expect(isAdminUser({ role: "ADMIN" })).toBe(true);
    expect(isAdminUser({ role: "USER" })).toBe(false);
    expect(isAdminUser({ role: "admin" })).toBe(false);
    expect(isAdminUser(null)).toBe(false);
  });
});
