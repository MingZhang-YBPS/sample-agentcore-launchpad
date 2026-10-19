import { describe, expect, it } from "vitest";

import en from "../locales/en/common.json";
import zh from "../locales/zh-CN/common.json";
import { alertDetailText } from "./alertDetail";

// a tiny interpolating `t` over one locale's `alertDetail` block
function tFor(bundle: { alertDetail: Record<string, string> }) {
  return (key: string, options: Record<string, unknown> = {}) => {
    const template = bundle.alertDetail[key.replace(/^alertDetail\./, "")];
    if (template === undefined) return key;
    return template.replace(/\{\{(\w+)\}\}/g, (_m, name: string) => String(options[name]));
  };
}

describe("alertDetailText", () => {
  const tZh = tFor(zh as never);
  const tEn = tFor(en as never);

  it("renders every backend template in Chinese", () => {
    expect(alertDetailText(tZh, "p95 over 171 traces")).toBe("171 条链路的 P95");
    expect(alertDetailText(tZh, "3/40 traces failed")).toBe("40 条链路中 3 条失败");
    expect(alertDetailText(tZh, "no traffic in this window")).toBe("这个时间窗口里没有流量");
    expect(alertDetailText(tZh, "12 score(s) over 4 session(s)")).toBe("4 个会话的 12 个分数");
    expect(alertDetailText(tZh, "month-to-date estimate from the price map")).toBe("本月至今按价格表估算");
    expect(alertDetailText(tZh, "month-to-date estimate from the price map (default $1.20, prod $0.40)"))
      .toBe("本月至今按价格表估算（default $1.20, prod $0.40）");
    expect(alertDetailText(tZh, "could not read telemetry: ClientError")).toBe("读取遥测失败：ClientError");
    expect(alertDetailText(tZh, "prod: could not estimate spend: Timeout")).toBe("prod: 估算花费失败：Timeout");
  });

  it("renders the drift-watch schedule lines", () => {
    expect(alertDetailText(tZh, "run 963b50cb65d0")).toBe("运行 963b50cb65d0");
    expect(alertDetailText(tZh, "project archived")).toBe("项目已归档");
    expect(alertDetailText(tZh, "estimated $0.73 exceeds the configured ceiling $0.5")).toBe("估算 $0.73 超过设定的上限 $0.5");
  });

  it("keeps the webhook suffix and translates both halves", () => {
    expect(alertDetailText(tZh, "p95 over 171 traces · webhook HTTP 200")).toBe("171 条链路的 P95 · Webhook HTTP 200");
  });

  it("is the backend line itself in English", () => {
    for (const line of ["p95 over 171 traces", "3/40 traces failed", "no online evaluation scores in the last 24 h",
      "month-to-date billed cost (Cost Explorer, launchpad:project)", "unknown kind 'x'"]) {
      expect(alertDetailText(tEn, line)).toBe(line);
    }
  });

  it("falls back to the original text for an unknown line", () => {
    expect(alertDetailText(tZh, "something new")).toBe("something new");
    expect(alertDetailText(tZh, null)).toBe("");
  });
});
