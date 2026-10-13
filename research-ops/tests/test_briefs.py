from __future__ import annotations

from research_ops.services.briefs import developer_instructions


def test_brief_does_not_generate_conversation_instructions() -> None:
    instructions = developer_instructions(
        {
            "work_id": "HARN-0001",
            "branch_name": "Main",
            "objective": "Verify the PPT trajectory.",
            "acceptance_criteria": [],
            "memory_context": [],
        }
    )

    assert instructions == ""
