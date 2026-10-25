import i18next, { type TFunction } from "i18next";
import { beforeAll, describe, expect, it } from "vitest";

import en from "../locales/en/common.json";
import zhCN from "../locales/zh-CN/common.json";

let tEn: TFunction;
let tZh: TFunction;

beforeAll(async () => {
  const make = async (lng: string) => {
    const inst = i18next.createInstance();
    await inst.init({
      lng,
      resources: { en: { common: en }, "zh-CN": { common: zhCN } },
      defaultNS: "common",
      interpolation: { escapeValue: false },
    });
    return inst.t;
  };
  tEn = await make("en");
  tZh = await make("zh-CN");
});

// the server-side golden-item checks (golden-import G4) answer with these codes
const CODES = ["golden.empty_input", "golden.empty_expected", "golden.unknown_criteria"];

describe("golden-set validation errors", () => {
  it("are translated in both locales", () => {
    for (const code of CODES) {
      const key = `apiErrors.${code}`;
      expect(tEn(key)).not.toBe(key);
      expect(tZh(key)).not.toBe(key);
      expect(tZh(key)).not.toBe(tEn(key));
    }
    expect(tEn("apiErrors.golden.empty_expected")).toContain("expected answer");
    expect(tZh("apiErrors.golden.empty_expected")).toContain("期望答案");
  });
});
