import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import i18next from "i18next";
import { beforeAll, describe, expect, it } from "vitest";

import { biasText, dlcText, findingText } from "./common";

const here = dirname(fileURLToPath(import.meta.url));
const read = (lang: string) => JSON.parse(readFileSync(resolve(here, `../../../locales/${lang}/common.json`), "utf-8"));
const zh = i18next.createInstance();

beforeAll(async () => {
  await zh.init({ lng: "zh-CN", resources: { "zh-CN": { translation: read("zh-CN") } }, keySeparator: ".", nsSeparator: false });
});
const t = (k: string, o?: Record<string, unknown>) => zh.t(k, o as never) as unknown as string;

describe("backend English copy reads in Chinese", () => {
  it("translates the gate, ladder and watch messages", () => {
    expect(dlcText(t, "below threshold")).toBe("低于阈值");
    expect(dlcText(t, "judge not calibrated — observed until calibrated")).toBe("裁判未校准——校准之前只作观察");
    expect(dlcText(t, "the runs used different criteria versions")).toBe("这些运行用的判据版本不同");
    expect(dlcText(t, "1 evaluation error(s)")).toBe("1 条评估出错");
    expect(dlcText(t, "2 of 15 expected items have no verdict")).toBe("2 / 15 个预期样本没有判定");
    expect(dlcText(t, "something new")).toBe("something new");
  });

  it("translates criteria findings, naming the missing dimension", () => {
    expect(findingText(t, { code: "criteria.dimension_missing", message: "no 'cost' criterion — add one or write 'n/a:cost <reason>' in a criterion note" }))
      .toBe("缺少「成本」维度的判据——添加一条，或在某条判据的备注里写 n/a:cost <原因>");
    expect(findingText(t, { code: "criteria.judge_needs_examples", message: "add at least one positive…" }))
      .toBe("至少加一个正例和一个反例——校准裁判靠的就是它们");
  });

  it("translates the golden-set source bias", () => {
    expect(biasText(t, "manual", "the writer knows the answer — items read cleaner than real traffic")).toBe("撰写人知道答案——样本比真实流量更干净");
  });

  it("has the same keys in English", () => {
    const en = read("en").v3.dlcText;
    const zhKeys = read("zh-CN").v3.dlcText;
    expect(Object.keys(en.finding).sort()).toEqual(Object.keys(zhKeys.finding).sort());
    expect(Object.keys(en.msg).sort()).toEqual(Object.keys(zhKeys.msg).sort());
    expect(read("zh-CN").apiErrors["criteria.invalid"]).toBe("判据表有错误");
  });
});
