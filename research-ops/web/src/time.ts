export const DISPLAY_TIME_ZONE = "Asia/Shanghai";
export const DISPLAY_TIME_ZONE_LABEL = "Shanghai (UTC+8)";

type DateInput = string | number | Date;

const DEFAULT_DATE_TIME_OPTIONS: Intl.DateTimeFormatOptions = {
  year: "numeric",
  month: "numeric",
  day: "numeric",
  hour: "numeric",
  minute: "2-digit",
  second: "2-digit",
};

export function formatDateTime(
  value: DateInput,
  options: Intl.DateTimeFormatOptions = {},
  locale?: string,
): string {
  const resolvedOptions = Object.keys(options).length ? options : DEFAULT_DATE_TIME_OPTIONS;
  return new Intl.DateTimeFormat(locale, {
    ...resolvedOptions,
    timeZone: DISPLAY_TIME_ZONE,
    hourCycle: "h23",
  }).format(toDate(value));
}

export function formatTime(value: DateInput, locale?: string): string {
  return formatDateTime(value, { hour: "2-digit", minute: "2-digit" }, locale);
}

export function formatDate(value: DateInput, locale?: string): string {
  return formatDateTime(value, { year: "numeric", month: "short", day: "numeric" }, locale);
}

/**
 * API timestamps may come from SQLite as naive UTC ISO strings. Treat those
 * values as UTC instead of letting the browser interpret them in its local
 * timezone. Live events already include a timezone suffix, so they remain
 * unchanged.
 */
export function timestampMillis(value: DateInput): number {
  return toDate(value).getTime();
}

function toDate(value: DateInput): Date {
  if (value instanceof Date || typeof value === "number") return new Date(value);
  const normalized = value.trim();
  const hasTimezone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(normalized);
  return new Date(hasTimezone ? normalized : `${normalized}Z`);
}
