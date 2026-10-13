-- Temporary live guard until the notification worker is safely restarted.
-- Preserve results and manual-turn notifications; suppress only automatic Goal turns.
BEGIN IMMEDIATE;
CREATE TRIGGER IF NOT EXISTS suppress_inactive_goal_turn_notification
AFTER INSERT ON turn_notifications
WHEN NEW.status IN ('pending', 'failed') AND EXISTS (
    SELECT 1 FROM codex_turns t
    JOIN execution_branches b ON b.id = t.branch_id
    LEFT JOIN native_goal_monitors m ON m.branch_id = t.branch_id
    WHERE t.id = NEW.turn_id
      AND json_extract(t.metadata_json, '$.prompt_kind') = 'goal_continuation'
      AND (
          (b.codex_thread_id IS NOT NULL AND b.codex_thread_id != ''
           AND b.codex_thread_id != t.thread_id)
          OR (m.thread_id = t.thread_id AND (
              json_extract(m.goal_snapshot, '$.status') = 'paused'
              OR m.execution_status IN ('cleared', 'superseded')
          ))
      )
)
BEGIN
    UPDATE turn_notifications SET status = 'suppressed', next_attempt_at = NULL,
        last_error = 'Automatic Goal turn belongs to a paused, cleared or superseded Goal'
    WHERE id = NEW.id;
END;
CREATE TRIGGER IF NOT EXISTS suppress_pending_goal_turns_on_pause
AFTER UPDATE OF goal_snapshot, execution_status ON native_goal_monitors
WHEN json_extract(NEW.goal_snapshot, '$.status') = 'paused'
     OR NEW.execution_status IN ('cleared', 'superseded')
BEGIN
    UPDATE turn_notifications SET status = 'suppressed', next_attempt_at = NULL,
        last_error = 'Automatic Goal turn belongs to a paused, cleared or superseded Goal'
    WHERE status IN ('pending', 'failed') AND turn_id IN (
        SELECT id FROM codex_turns
        WHERE branch_id = NEW.branch_id AND thread_id = NEW.thread_id
          AND json_extract(metadata_json, '$.prompt_kind') = 'goal_continuation'
    );
END;
UPDATE native_goal_monitors SET execution_status = execution_status
WHERE json_extract(goal_snapshot, '$.status') = 'paused'
   OR execution_status IN ('cleared', 'superseded');
COMMIT;
