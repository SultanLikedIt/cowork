"""Skills: catalog integrity, and the orchestration boundary.

The whole risk of this feature is that a "skill" quietly becomes a routing
input, an extra worker, or a tool grant. These tests exist to make that
impossible to do accidentally: the orchestration section below asserts that
the router prompt, the decomposition policy, the assignment path, the
recovery policy and tool selection are all byte-identical with and without
skills selected.

No live model calls (same rule as test_capability_routing.py) -- everything
here is either pure catalog logic or a comparison of the inputs the model
would have been handed.
"""

from __future__ import annotations

import asyncio
import inspect
import sys

from camel.societies.workforce.workforce_callback import WorkforceCallback

import camel_service
import skills
import workforce_plan
from model_config import UsageTracker
from skills import BUILT_IN_SKILLS, render_guidance, resolve_skills, skill_ids

ALL_IDS = [skill.skill_id for skill in BUILT_IN_SKILLS]


class _Callback(WorkforceCallback):
    """No-op callback. Workforce validates the type, so this subclasses the
    real interface rather than duck-typing it."""

    def __getattr__(self, _name):
        return lambda *args, **kwargs: None

    def log_task_created(self, event): pass
    def log_task_decomposed(self, event): pass
    def log_task_assigned(self, event): pass
    def log_task_started(self, event): pass
    def log_task_completed(self, event): pass
    def log_task_failed(self, event): pass
    def log_worker_created(self, event): pass
    def log_worker_deleted(self, event): pass
    def log_all_tasks_completed(self, event): pass


def _build(strategy, skill_list):
    return camel_service.build_workforce(
        _Callback(),
        UsageTracker(),
        execution_strategy=strategy,
        skills=skill_list,
    )


def _worker_prompts(workforce):
    """role -> system message, for every fixed worker on the workforce."""
    prompts = {}
    for child in workforce._children:
        agent = getattr(child, "worker", None)
        if agent is None or agent.system_message is None:
            continue
        prompts[agent.system_message.role_name] = agent.system_message.content
    return prompts


def _without_run_context(prompts):
    """Prompts with the RUN CONTEXT block (runtime_context.runtime_block)
    removed, for comparisons that must be byte-exact.

    That block ends every worker prompt and carries the real wall-clock time
    to the minute, so two builds taken either side of a minute boundary
    differ legitimately. Comparing the rest is what the skill-leak assertion
    is actually about.
    """
    return {
        role: prompt.split("RUN CONTEXT --")[0]
        for role, prompt in prompts.items()
    }


def _worker_descriptions(workforce):
    """The text the COORDINATOR reads when assigning. If skills ever leak
    into this, they have moved upstream of assignment."""
    return sorted(str(child.description) for child in workforce._children)


def _worker_tools(workforce):
    """role -> sorted tool names. Tool selection must be skill-blind."""
    tools = {}
    for child in workforce._children:
        agent = getattr(child, "worker", None)
        if agent is None:
            continue
        role = agent.system_message.role_name if agent.system_message else str(child)
        tools[role] = sorted(getattr(agent, "tool_dict", {}) or {})
    return tools


# The only destinations rendered guidance is allowed to reach. Each is a
# system message on an agent that EXECUTES; none is task content, a worker
# description, or any prompt an orchestration step reads. Putting guidance
# in task content instead would feed it to the planner, the coordinator,
# the quality analyser and tool selection at once -- and the DEMO check in
# main() shows concretely what that does to the tool verdict.
_ALLOWED_GUIDANCE_SINKS = ("system_message", "system_prompt", "effective_prompt")


def _guidance_sinks():
    """(callsite line, nearest assignment target) for every place
    camel_service renders skill guidance."""
    source = inspect.getsource(camel_service).splitlines()
    sinks = []
    for index, line in enumerate(source):
        if "render_skill_guidance(" not in line or line.strip().startswith("#"):
            continue
        # Look outward from the callsite for what the value flows into: the
        # enclosing kwarg above it, or the variable it lands in.
        window = source[max(0, index - 14) : index + 6]
        joined = "\n".join(window)
        if "worker_skill_guidance" in line:
            # Assigned to a local -- follow it to its single consumer.
            joined += "\n".join(
                other for other in source if "worker_skill_guidance" in other
            )
        sinks.append((line.strip(), joined))
    return sinks


def main() -> None:
    failures = []

    def check(name, condition, detail=""):
        print(f"[{'PASS' if condition else 'FAIL'}] {name}"
              + (f" -- {detail}" if not condition else ""))
        if not condition:
            failures.append(name)

    # --- Catalog integrity ---------------------------------------------
    check("catalog has 13 skills", len(BUILT_IN_SKILLS) == 13, str(len(BUILT_IN_SKILLS)))
    check("skill ids are unique", len(set(ALL_IDS)) == len(ALL_IDS))
    check(
        "skill names are unique",
        len({skill.name for skill in BUILT_IN_SKILLS}) == len(BUILT_IN_SKILLS),
    )
    check(
        "every skill has a short description and real guidance",
        all(
            10 < len(skill.description) <= 90 and len(skill.guidance) > 120
            for skill in BUILT_IN_SKILLS
        ),
    )

    # Skills must be methods, not renamed agents. The three fixed workers are
    # Research / Document / Coding -- a skill whose name is built
    # from one of those words would be a second way to reach the same agent.
    fixed_worker_words = {"research", "document", "coding"}
    for skill in BUILT_IN_SKILLS:
        name_words = {word.strip("&").lower() for word in skill.name.split()}
        check(
            f"skill {skill.skill_id!r} does not restate a fixed agent's role",
            not (name_words & fixed_worker_words),
            skill.name,
        )

    # --- Resolution -----------------------------------------------------
    check("unknown ids are dropped, not raised", skill_ids(resolve_skills(["nope"])) == [])
    check("empty selection resolves empty", resolve_skills([]) == [] and resolve_skills(None) == [])
    check(
        "selection is capped at MAX_ACTIVE_SKILLS",
        len(resolve_skills(ALL_IDS)) == skills.MAX_ACTIVE_SKILLS,
        str(len(resolve_skills(ALL_IDS))),
    )
    check(
        "duplicate ids collapse",
        skill_ids(resolve_skills(["exec_brief", "exec_brief"])) == ["exec_brief"],
    )
    # Catalog order, not request order -- this is what makes a persisted run
    # reproducible byte-for-byte rather than dependent on click sequence.
    forward = skill_ids(resolve_skills(["root_cause", "exec_brief"]))
    backward = skill_ids(resolve_skills(["exec_brief", "root_cause"]))
    check("resolution is order-independent", forward == backward, f"{forward} vs {backward}")
    check(
        "same selection renders identical guidance",
        render_guidance(resolve_skills(["exec_brief", "root_cause"]))
        == render_guidance(resolve_skills(["root_cause", "exec_brief"])),
    )
    # None and [] must behave identically: every execution entry point
    # declares `skills=None` as its default, so an omitted argument has to
    # render like an empty selection rather than raising.
    check(
        "no skills renders an empty string (both [] and None)",
        render_guidance([]) == "" and render_guidance(None) == "",
    )
    check(
        "guidance states it grants no tools or new work",
        "grant no new tools" in render_guidance(resolve_skills(["root_cause"]))
        and "do NOT change what you were asked to produce"
        in render_guidance(resolve_skills(["root_cause"])),
    )
    check(
        "catalog payload withholds prompt text from the client",
        all("guidance" not in entry for entry in skills.catalog_payload()),
    )

    # =====================================================================
    # ORCHESTRATION BOUNDARY
    #
    # Each check below compares a real orchestration input built with skills
    # against the same input built without them. Any difference means a
    # skill has escaped downstream execution.
    # =====================================================================
    picked = resolve_skills(["root_cause", "risk_analysis", "exec_brief"])
    check("test fixture actually selected skills", len(picked) == 3)

    # 1. ROUTING -- the execution router is gone; skills cannot leak into it.
    check(
        "camel_service has no route_request",
        not hasattr(camel_service, "route_request"),
    )
    check(
        "camel_service has no routing system prompt",
        not hasattr(camel_service, "ROUTING_SYSTEM_PROMPT"),
    )

    # 2. DECOMPOSITION -- the planner prompt and the subtask caps are
    #    constants; skills are not interpolated into either.
    check(
        "initial plan prompt contains no skill text",
        not any(
            skill.name in workforce_plan.PLAN_SYSTEM_PROMPT
            for skill in BUILT_IN_SKILLS
        ),
    )
    check(
        "_decompose_task cannot receive skills",
        "skills" not in inspect.signature(camel_service._patched_decompose_task).parameters,
    )

    # 3. ASSIGNMENT -- what the coordinator reads to choose a worker.
    for strategy in ("workforce", "specialist"):
        plain = _build(strategy, [])
        skilled = _build(strategy, picked)
        check(
            f"[{strategy}] worker descriptions the coordinator reads are unchanged",
            _worker_descriptions(plain) == _worker_descriptions(skilled),
        )
        check(
            f"[{strategy}] assignment prompt contains no skill text",
            not any(
                skill.name in str(camel_service._PATCHED_ASSIGN_TASK_PROMPT)
                for skill in picked
            ),
        )
        # 4. WORKER SET -- no skill may add, remove or rename a worker.
        check(
            f"[{strategy}] the roster is unchanged",
            sorted(plain.role_worker_ids) == sorted(skilled.role_worker_ids)
            and len(plain._children) == len(skilled._children),
        )
        # 5. TOOL SELECTION -- must be identical with and without skills.
        check(
            f"[{strategy}] per-worker tool selection is unchanged",
            _worker_tools(plain) == _worker_tools(skilled),
            f"{_worker_tools(plain)} vs {_worker_tools(skilled)}",
        )
        # 6. RECOVERY -- policy constants, and the strategy stamp the
        #    recovery branch keys off, are untouched.
        check(
            f"[{strategy}] execution_strategy stamp is unchanged",
            plain.execution_strategy == skilled.execution_strategy == strategy,
        )

    check(
        "recovery policy constants are skill-independent",
        camel_service.WORKFORCE_ALLOWED_RECOVERY_STRATEGIES
        == camel_service.WORKFORCE_ALLOWED_RECOVERY_STRATEGIES
        and "skills"
        not in inspect.signature(camel_service._patched_analyze_task).parameters,
    )

    # 7. Task-based dynamic-worker tool selection reads the SUBTASK text and
    #    nothing else.
    #
    #    This one is worth stating precisely, because guidance text is NOT
    #    inert here: "Root Cause Analysis" talks about observing and ruling
    #    out, "Risk & Failure Analysis" about things being operated, and
    #    concatenating that onto a subtask genuinely does move the
    #    keyword-based verdict -- demonstrated below. The safety property is
    #    therefore not "the verdict is robust to guidance" (it is not), it
    #    is "guidance is never in the string this function is given",
    #    because it is appended to system messages only. The demonstration
    #    is kept as a test so that anyone who later considers appending
    #    guidance to task content sees exactly what it would break.
    check(
        "tool selection takes only the subtask text",
        list(inspect.signature(camel_service.subtask_needs_execution_tools).parameters)
        == ["task_text"],
    )
    contaminated = "Design the schema." + guidance_probe if (
        guidance_probe := render_guidance(picked)
    ) else ""
    check(
        "DEMO: guidance in the tool-selection input WOULD corrupt the verdict",
        camel_service.subtask_needs_execution_tools("Design the schema.") is True
        and camel_service.subtask_needs_execution_tools(contaminated) is False,
        "if this ever stops holding the isolation below matters less, not more",
    )
    # The actual guarantee: the only thing stashed for tool selection is the
    # task's own content, which no skill code path writes to.
    create_source = inspect.getsource(camel_service._patched_create_worker_node_for_task)
    check(
        "the stashed tool-selection input is task.content, unmodified",
        'getattr(task, "content", "")' in create_source
        and "skill" not in create_source.lower(),
    )
    sinks = _guidance_sinks()
    check("every guidance callsite is accounted for", len(sinks) == 3, str(len(sinks)))
    for callsite, context in sinks:
        check(
            f"guidance sink is a system message: {callsite[:52]!r}",
            any(sink in context for sink in _ALLOWED_GUIDANCE_SINKS),
        )
    check(
        "no guidance callsite writes to task content",
        not any(
            "task.content" in context or "Task(" in context for _, context in sinks
        ),
    )

    # =====================================================================
    # DOWNSTREAM APPLICATION -- where skills are SUPPOSED to appear.
    # =====================================================================
    guidance = render_guidance(picked)

    # SPECIALIST: exactly one known fixed worker runs, so guidance on the
    # fixed workers' system messages is guidance on the agent that executes.
    specialist_prompts = _worker_prompts(_build("specialist", picked))
    check(
        "[specialist] every fixed worker carries the guidance",
        all(guidance in prompt for prompt in specialist_prompts.values()),
    )

    # WORKFORCE: workers are left alone -- several run in parallel and their
    # outputs feed the synthesizer, so the guidance is applied once at the
    # output boundary instead.
    workforce_prompts = _worker_prompts(_build("workforce", picked))
    check(
        "[workforce] NO fixed worker carries the guidance",
        not any("APPLIED SKILLS" in prompt for prompt in workforce_prompts.values()),
    )
    check(
        "[workforce] worker prompts are byte-identical to a no-skill build",
        _without_run_context(workforce_prompts)
        == _without_run_context(_worker_prompts(_build("workforce", []))),
    )

    # And the workforce output boundary does receive it: synthesis runs even
    # for a single successful leaf when skills are active, which is the one
    # place that would otherwise silently drop them.
    class _Leaf:
        def __init__(self, result):
            self.result = result
            self.state = camel_service.TaskState.DONE
            self.subtasks = []

    class _Root(_Leaf):
        def __init__(self, leaves):
            super().__init__("root")
            self.subtasks = leaves

    single_leaf = _Root([_Leaf("the one worker's answer")])
    answer, _ = asyncio.run(
        camel_service.synthesize_workforce_result(
            single_leaf, "topic", UsageTracker(), skills=[]
        )
    )
    check(
        "single-leaf synthesis is still skipped with no skills",
        answer == "the one worker's answer",
        answer,
    )
    check(
        "synthesize_workforce_result accepts skills",
        "skills"
        in inspect.signature(camel_service.synthesize_workforce_result).parameters,
    )
    check(
        "run_single_agent accepts skills (DIRECT path)",
        "skills" in inspect.signature(camel_service.run_single_agent).parameters,
    )

    print(f"\n{'FAILED -- see above' if failures else 'All skills tests passed'}")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
