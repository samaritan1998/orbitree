import { expect, test } from "vitest";
import { DISPLAY_TIME_ZONE, formatDateTime, timestampMillis } from "./time";

test("uses Shanghai time for displayed timestamps", () => {
  expect(DISPLAY_TIME_ZONE).toBe("Asia/Shanghai");
  expect(formatDateTime(
    "2026-07-17T00:00:00Z",
    { hour: "2-digit", minute: "2-digit", hour12: false },
    "en-GB",
  )).toBe("08:00");
});

test("treats timezone-less API timestamps as UTC", () => {
  const naive = "2026-07-17T05:48:15.601113";
  expect(timestampMillis(naive)).toBe(timestampMillis(`${naive}Z`));
  expect(formatDateTime(
    naive,
    { hour: "2-digit", minute: "2-digit", hour12: false },
    "en-GB",
  )).toBe("13:48");
});
