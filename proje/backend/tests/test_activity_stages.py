"""Every activity stage a worker can emit must survive the job snapshot.

"coding" was added to ActivityEvent only; main.ActivityStatus kept its own
list, so each snapshot of a Coding job raised a validation error (500 on
GET /jobs and the SSE stream) and the UI showed no output for the whole run
while the job itself finished in seconds.
"""

from __future__ import annotations

import typing

import pytest

import main
from workforce.events import ActivityEvent, ActivityStage


@pytest.mark.parametrize("stage", typing.get_args(ActivityStage))
def test_every_emitted_stage_is_accepted_by_the_snapshot_model(stage: str) -> None:
    event = ActivityEvent(task_id="t", stage=stage, detail="x")
    status = main.ActivityStatus(task_id=event.task_id, stage=event.stage, detail=event.detail)
    assert status.stage == stage


def test_the_two_models_share_one_list() -> None:
    assert main.ActivityStatus.model_fields["stage"].annotation is ActivityStage
    assert ActivityEvent.model_fields["stage"].annotation is ActivityStage
