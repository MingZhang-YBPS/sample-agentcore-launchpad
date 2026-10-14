import { describe, expect, it } from "vitest";

import { ApiError } from "../lib/api";
import { toastDetail } from "./toast";

const aws = new ApiError(
  "aws.validation",
  "AWS rejected the request: bad role",
  { aws_error_code: "ValidationException", operation: "CreateGateway" },
  { rawMessage: "bad role", title: "AWS rejected the request", requestId: "rid-1" },
);

describe("toastDetail (projects §19)", () => {
  it("keeps the AWS facts and request id, titled with the toast's own text", () => {
    const d = toastDetail("act", "Action failed: AWS rejected the request: bad role", aws);
    expect(d).toBeInstanceOf(ApiError);
    expect(d?.title).toBe("Action failed: AWS rejected the request: bad role");
    expect(d?.requestId).toBe("rid-1");
    expect(d?.rawMessage).toBe("bad role");
    expect(d?.detail).toEqual(aws.detail);
    expect(d?.at).toBe(aws.at);
    // the caller's error is left as it was
    expect(aws.title).toBe("AWS rejected the request");
  });

  it("a request id alone is enough", () => {
    expect(toastDetail("act", "x", new ApiError("workspace.x", "m", null, { requestId: "rid-2" }))?.requestId).toBe("rid-2");
  });

  it("stays a plain toast otherwise", () => {
    expect(toastDetail("ok", "done", aws)).toBeNull();
    expect(toastDetail("act", "x", undefined)).toBeNull();
    expect(toastDetail("act", "x", new Error("boom"))).toBeNull();
    expect(toastDetail("act", "x", new ApiError("agent.name_exists", "taken", null))).toBeNull();
  });
});
