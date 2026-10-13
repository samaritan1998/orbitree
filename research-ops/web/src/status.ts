import type { Branch, ConversationMessage, ExternalJob, Session, WorkItem } from "./types";

export type OperationalStatus =
  | "Running"
  | "Expired"
  | "Ready"
  | "Blocked"
  | "Waiting external"
  | "Needs decision"
  | "Review"
  | "Done"
  | "Cancelled"
  | "Parked"
  | "Continued"
  | "Provisioning"
  | "Error";

export type SessionStatus = "Running" | "Expired" | "Completed" | "Error" | "Not started";

export function timestampMillis(value: string): number {
  const hasTimezone = /(?:Z|[+-]\d{2}:\d{2})$/i.test(value);
  return new Date(hasTimezone ? value : `${value}Z`).getTime();
}

export function conversationTurnPending(
  messages: Array<Pick<ConversationMessage, "role" | "metadata_json">>,
): boolean {
  const latestUserMessage = [...messages].reverse().find((message) => message.role === "user");
  return latestUserMessage?.metadata_json.send_status === "pending";
}

export function externalJobActive(status: ExternalJob["status"]): boolean {
  const normalized = status.trim().toLowerCase().replaceAll("-", "_").replaceAll(" ", "_");
  return ["created", "pending", "queued", "submitted", "starting", "running", "in_progress"]
    .some((candidate) => normalized === candidate || normalized.startsWith(`${candidate}_`));
}

export function operationalStatus({
  work,
  branch,
  session,
  externalJobs = [],
}: {
  work: WorkItem;
  branch?: Branch;
  session?: Session;
  externalJobs?: Array<Pick<ExternalJob, "status">>;
}): OperationalStatus {
  if (work.lifecycle === "done") return "Done";
  if (work.lifecycle === "cancelled") return "Cancelled";
  if (branch?.status === "continued") return "Continued";
  if (branch?.status === "provisioning") return "Provisioning";
  if (branch?.status === "blocked") return "Blocked";
  if (work.flow === "blocked") return "Blocked";
  if (work.flow === "waiting_external") return "Waiting external";
  if (work.flow === "needs_decision") return "Needs decision";
  if (work.lifecycle === "review") return "Review";
  if (session?.status === "active" && timestampMillis(session.lease_expires_at) > Date.now()) {
    return "Running";
  }
  if (work.flow === "running" || externalJobs.some((job) => externalJobActive(job.status))) {
    return "Running";
  }
  if (session?.status === "error") return "Error";
  if (session?.status === "expired" || session?.status === "active") return "Expired";
  if (branch?.status === "parked") return "Parked";
  if (["abandoned", "expired"].includes(branch?.status ?? "")) return "Expired";
  return "Ready";
}

export function sessionStatus(session?: Session): SessionStatus {
  if (!session) return "Not started";
  if (
    ["starting", "active"].includes(session.status)
    && timestampMillis(session.lease_expires_at) > Date.now()
  ) {
    return "Running";
  }
  if (["starting", "active", "expired"].includes(session.status)) return "Expired";
  if (session.status === "error") return "Error";
  return "Completed";
}
