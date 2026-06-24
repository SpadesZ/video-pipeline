import logging
import asyncio
from pipeline.models.cue_ledger import AssetType
from pipeline.models.production_artifact import ProductionArtifact
from pipeline.models.visual_contract import (
    CharacterProfile,
    ShotSpec,
    ShotType,
    VisualQualityContract,
    VisualQualityFinding,
    VisualQualityReport,
    VisualStyleGuide,
)
from pipeline.adapters.llm.lava_settings import get_llm_brain_status
from pipeline.stages.llm_executors import (
    run_visual_bible,
    run_storyboard_candidates,
    run_quality_review,
)

logger = logging.getLogger("Visual_Contract_Builder")
logger.setLevel(logging.INFO)

DEFAULT_NEGATIVE_PROMPTS = [
    "low resolution",
    "blurry faces",
    "extra fingers",
    "warped hands",
    "mismatched character identity",
    "unreadable text",
    "watermark",
    "logo artifacts",
    "overly glossy AI look",
]


def map_shot_type(asset_type: AssetType) -> ShotType:
    if asset_type == AssetType.GENERATED_IMAGE:
        return ShotType.GENERATED_IMAGE
    if asset_type == AssetType.BROLL:
        return ShotType.BROLL
    if asset_type == AssetType.SCREENCAST:
        return ShotType.SCREENCAST
    if asset_type == AssetType.TITLE_CARD:
        return ShotType.TITLE_CARD
    return ShotType.UNKNOWN


def map_str_to_shot_type(val: str) -> ShotType:
    val = val.lower().strip()
    if "image" in val or "generated" in val:
        return ShotType.GENERATED_IMAGE
    if "broll" in val or "b-roll" in val:
        return ShotType.BROLL
    if "screen" in val or "screencast" in val:
        return ShotType.SCREENCAST
    if "title" in val or "card" in val:
        return ShotType.TITLE_CARD
    if "talking" in val or "host" in val:
        return ShotType.TALKING_HEAD
    return ShotType.UNKNOWN


def build_fallback_prompt(artifact: ProductionArtifact, cue) -> str:
    visual = cue.visual_prompt or cue.voice_text
    return (
        f"{artifact.title}. Scene: {visual}. "
        "Professional documentary explainer frame, clear focal subject, "
        "consistent palette, readable composition, no misleading or copyrighted branding."
    )


def build_fallback_contract(
    artifact: ProductionArtifact,
) -> tuple[VisualQualityContract, VisualQualityReport]:
    """Offline rule-based fallback when LLM keys are missing."""
    style = VisualStyleGuide(negative_prompts=DEFAULT_NEGATIVE_PROMPTS)
    characters = [
        CharacterProfile(
            character_id="host_01",
            role=artifact.persona or "operator",
            description="Consistent host voice and visual identity across generated and edited scenes.",
            continuity_notes=[
                "Keep clothing, age, hair, and face consistent if a human character is shown.",
                "Use the same color palette and caption treatment across all shots.",
            ],
        )
    ]

    shots: list[ShotSpec] = []
    findings: list[VisualQualityFinding] = []
    for cue in artifact.cue_ledger.cues if artifact.cue_ledger else []:
        shot_type = map_shot_type(cue.asset_type)
        prompt = build_fallback_prompt(artifact, cue)
        
        # Auto-fill visual prompt in cue ledger if empty
        if not cue.visual_prompt:
            cue.visual_prompt = prompt[:200]

        checks = [
            "No unreadable text or fake UI claims.",
            "No watermark or visible copyrighted marks unless licensed.",
            "Caption safe area remains clear.",
        ]
        if cue.asset_type == AssetType.GENERATED_IMAGE:
            checks.extend([
                "Character identity remains consistent.",
                "Hands, face, and eye direction are not distorted.",
                "Image does not look like generic AI stock art.",
            ])

        if cue.asset_type != AssetType.NONE and not cue.visual_prompt:
            findings.append(
                VisualQualityFinding(
                    code="missing_visual_prompt",
                    severity="blocker",
                    cue_id=cue.cue_id,
                    message="Cue needs a visual prompt before asset production.",
                )
            )

        shots.append(
            ShotSpec(
                cue_id=cue.cue_id,
                shot_type=shot_type,
                prompt=prompt,
                camera="static clean frame",
                composition="simple frame with clear negative space",
                continuity="Match style guide",
                negative_prompt=", ".join(DEFAULT_NEGATIVE_PROMPTS),
                quality_checks=checks,
            )
        )

    score = max(0, 100 - sum(25 if str(f.severity).lower() == "blocker" else 8 for f in findings))
    return (
        VisualQualityContract(
            project_id=artifact.project_id,
            style_guide=style,
            characters=characters,
            shots=shots,
        ),
        VisualQualityReport(project_id=artifact.project_id, score=score, findings=findings),
    )


async def build_visual_contract(
    artifact: ProductionArtifact,
) -> tuple[VisualQualityContract, VisualQualityReport]:
    """
    Build the Visual Quality Contract and Quality Report.
    If LLM API keys are active, calls LAVA storyboard with consensus optimization (N-best selection).
    Otherwise, gracefully falls back to the deterministic offline builder.
    """
    status = get_llm_brain_status()
    # Check if we have configured keys (OpenRouter or Google)
    has_active_keys = len(status.configured_env_keys) > 0

    if not has_active_keys:
        logger.info("No active LLM API keys detected. Using offline fallback VQC builder.")
        return build_fallback_contract(artifact)

    logger.info("Active LLM API keys found. Initiating LAVA VQC builder with Consensus Optimization...")
    try:
        # 1. Run Visual Bible
        bible_data = await run_visual_bible(
            artifact.title,
            artifact.genre or "tech",
            artifact.persona or "operator"
        )
        
        style_guide_data = bible_data.get("style_guide", {})
        style = VisualStyleGuide(
            style_name=style_guide_data.get("style_name", "documentary explainer"),
            aspect_ratio=style_guide_data.get("aspect_ratio", "16:9"),
            lighting=style_guide_data.get("lighting", "clean soft contrast"),
            color_grade=style_guide_data.get("color_grade", "natural contrast"),
            typography=style_guide_data.get("typography", "sans-serif"),
            negative_prompts=style_guide_data.get("negative_prompts", DEFAULT_NEGATIVE_PROMPTS)
        )
        
        characters = []
        for char in bible_data.get("characters", []):
            characters.append(
                CharacterProfile(
                    character_id=char.get("character_id", "host_01"),
                    role=char.get("role", "host"),
                    description=char.get("description", "A standard speaker"),
                    continuity_notes=char.get("continuity_notes", [])
                )
            )

        shots: list[ShotSpec] = []
        all_findings: list[VisualQualityFinding] = []

        cues = artifact.cue_ledger.cues if artifact.cue_ledger else []
        sem = asyncio.Semaphore(5)  # Restrict concurrent cues to 5
        
        async def process_cue(cue):
            async with sem:
                # 2. Generate 3 candidate storyboard specs
                try:
                    candidates = await run_storyboard_candidates(cue.cue_id, cue.voice_text, bible_data)
                except Exception as e:
                    logger.warning(f"Failed to generate candidates for cue {cue.cue_id}: {e}")
                    candidates = []
                
                best_candidate = None
                best_score = -1
                best_findings = []
                
                if candidates:
                    # Evaluate candidates concurrently
                    reviews = await asyncio.gather(*[
                        run_quality_review(cue.cue_id, cue.voice_text, cand, bible_data)
                        for cand in candidates
                    ], return_exceptions=True)
                    
                    for cand, review in zip(candidates, reviews):
                        if isinstance(review, Exception):
                            logger.warning(f"Quality review failed for candidate of cue {cue.cue_id}: {review}")
                            continue
                        score = review.get("score", 70)
                        findings = review.get("findings", [])
                        if score > best_score:
                            best_score = score
                            best_candidate = cand
                            best_findings = findings
                
                if not best_candidate:
                    # Fallback if no candidate generation succeeded
                    best_candidate = {
                        "shot_type": "generated_image",
                        "prompt": build_fallback_prompt(artifact, cue),
                        "camera": "static clean frame",
                        "composition": "centered safe zone",
                        "continuity": "match guide",
                        "negative_prompt": ", ".join(style.negative_prompts),
                        "quality_checks": ["Caption safe area is clear"]
                    }
                    best_score = 70
                    best_findings = []
                
                return cue, best_candidate, best_findings

        # Gather all tasks concurrently
        results = await asyncio.gather(*[process_cue(cue) for cue in cues])

        for cue, best_candidate, best_findings in results:
            # Update the cue ledger visual prompt with the best LLM generated prompt (fallback to default if empty)
            prompt_text = best_candidate.get("prompt") or build_fallback_prompt(artifact, cue)
            cue.visual_prompt = prompt_text
            cue.asset_type = AssetType.GENERATED_IMAGE  # Upgrade to visual generation
            
            shot_type = map_str_to_shot_type(best_candidate.get("shot_type", "generated_image"))
            
            shots.append(
                ShotSpec(
                    cue_id=cue.cue_id,
                    shot_type=shot_type,
                    prompt=prompt_text,
                    camera=best_candidate.get("camera", "medium shot"),
                    composition=best_candidate.get("composition", "rule of thirds"),
                    continuity=best_candidate.get("continuity", "consistent colors"),
                    negative_prompt=best_candidate.get("negative_prompt", ""),
                    quality_checks=best_candidate.get("quality_checks", [])
                )
            )
            
            for f in best_findings:
                all_findings.append(
                    VisualQualityFinding(
                        code=f.get("code", "quality_issue"),
                        severity=str(f.get("severity", "warning")).lower(),
                        cue_id=cue.cue_id,
                        message=f.get("message", "Issues found during AI review")
                    )
                )

        # Calculate final overall score
        blocker_count = sum(1 for f in all_findings if str(f.severity).lower() == "blocker")
        warning_count = sum(1 for f in all_findings if str(f.severity).lower() == "warning")
        overall_score = max(0, 100 - (blocker_count * 20) - (warning_count * 5))

        return (
            VisualQualityContract(
                project_id=artifact.project_id,
                style_guide=style,
                characters=characters,
                shots=shots,
            ),
            VisualQualityReport(project_id=artifact.project_id, score=overall_score, findings=all_findings),
        )

    except Exception as e:
        logger.error(f"LAVA Storyboard pipeline failed with exception: {e}. Falling back to offline builder.")
        return build_fallback_contract(artifact)
