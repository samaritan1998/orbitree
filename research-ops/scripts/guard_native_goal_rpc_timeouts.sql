-- Temporary SQLite guard while old workers await a safe restart.
-- Never change Codex Goal snapshots or retry genuine execution failures.
BEGIN IMMEDIATE;
CREATE TRIGGER IF NOT EXISTS prevent_native_goal_rpc_timeout_recovery
AFTER UPDATE OF last_error, retryable_error, next_retry_at ON native_goal_monitors
WHEN NEW.last_error IN (
    'Codex request timed out: thread/goal/get',
    'Codex request timed out: thread/read',
    'Codex request timed out: thread/turns/list',
    'Codex request timed out: thread/resume'
)
AND (NEW.retryable_error = 1 OR NEW.next_retry_at IS NOT NULL)
BEGIN
    UPDATE native_goal_monitors
    SET retryable_error = 0,
        next_retry_at = NULL,
        execution_status = CASE
            WHEN json_extract(goal_snapshot, '$.status') = 'active' THEN 'unavailable'
            ELSE coalesce(json_extract(goal_snapshot, '$.status'), 'unknown')
        END
    WHERE branch_id = NEW.branch_id;
END;
UPDATE native_goal_monitors
SET retryable_error = retryable_error
WHERE last_error IN (
    'Codex request timed out: thread/goal/get',
    'Codex request timed out: thread/read',
    'Codex request timed out: thread/turns/list',
    'Codex request timed out: thread/resume'
);
COMMIT;
