from pipeline.models.llm_control import LLMCapability, LLMTask


VIDEO_LLM_TASKS = [
    LLMTask(
        task_id="topic_research",
        label="Topic Research",
        capability=LLMCapability.CHAT,
        required=False,
        category="planning",
        fallback_behavior="Return deterministic niche suggestions and competitor gaps when no provider is configured.",
        description="Find niche angles, competitor gaps, and search intent before scripting.",
    ),
    LLMTask(
        task_id="script_outline",
        label="Script Outline",
        capability=LLMCapability.CHAT,
        required=False,
        category="script",
        fallback_behavior="Use local script template and existing user prompt when live LLM calls fail.",
        description="Turn a chosen angle into a structured video script with markers.",
    ),
    LLMTask(
        task_id="visual_bible",
        label="Visual Bible",
        capability=LLMCapability.CHAT,
        required=True,
        category="visual",
        fallback_behavior="Use offline visual contract builder with conservative style defaults.",
        description="Create the project visual style, character continuity, and prompt constraints.",
    ),
    LLMTask(
        task_id="storyboard",
        label="Storyboard",
        capability=LLMCapability.CHAT,
        required=True,
        category="visual",
        fallback_behavior="Use deterministic cue-to-shot prompts when consensus storyboard calls fail.",
        description="Convert transcript cues into shot specs, prompts, and negative prompts.",
    ),
    LLMTask(
        task_id="packaging",
        label="Packaging",
        capability=LLMCapability.CHAT,
        required=False,
        category="packaging",
        fallback_behavior="Use local title, description, disclosure, and chapter defaults.",
        description="Generate title, thumbnail concept, description, chapters, and disclosures.",
    ),
    LLMTask(
        task_id="quality_review",
        label="Quality Review",
        capability=LLMCapability.CHAT,
        required=True,
        category="quality",
        fallback_behavior="Use deterministic quality checks and keep human review as final gate.",
        description="Review the preview package against visual, originality, and platform quality gates.",
    ),
]

TASK_IDS = {task.task_id for task in VIDEO_LLM_TASKS}
