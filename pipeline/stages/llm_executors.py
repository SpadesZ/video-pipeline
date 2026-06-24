import json
import re
import logging
from typing import Any, Dict, List, Tuple
from pydantic import BaseModel

from pipeline.adapters.llm.lava_dispatcher import dispatch_llm_task
from pipeline.models.cue_ledger import AssetType, CueItem
from pipeline.models.visual_contract import (
    VisualStyleGuide,
    CharacterProfile,
    ShotSpec,
    ShotType,
    VisualQualityFinding,
)

logger = logging.getLogger("LLM_Executors")
logger.setLevel(logging.INFO)


def extract_json(text: str) -> str:
    """Extract raw JSON string from LLM response text, handling markdown blocks."""
    # Find markdown block ```json ... ```
    match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    # Find generic markdown block ``` ... ```
    match = re.search(r"```\s*(.*?)\s*```", text, re.DOTALL)
    if match:
        return match.group(1).strip()

    # If no markdown code block is found, try to locate the outermost curly braces or square brackets
    text_stripped = text.strip()
    first_brace = text_stripped.find('{')
    last_brace = text_stripped.rfind('}')
    first_bracket = text_stripped.find('[')
    last_bracket = text_stripped.rfind(']')
    
    if first_brace != -1 and last_brace != -1:
        if first_bracket == -1 or first_brace < first_bracket:
            return text_stripped[first_brace:last_brace + 1].strip()
    if first_bracket != -1 and last_bracket != -1:
        return text_stripped[first_bracket:last_bracket + 1].strip()
        
    return text_stripped


def safe_parse_json(text: str) -> Any:
    """Safely parse LLM output text as JSON."""
    cleaned = extract_json(text)
    try:
        return json.loads(cleaned)
    except Exception as e:
        logger.error(f"Failed to parse JSON: {e}. Raw content: {text}")
        raise ValueError(f"LLM output is not valid JSON: {str(e)}")


async def run_topic_research(topic_prompt: str) -> Dict[str, Any]:
    """
    LAVA Task: topic_research
    Find niche angles, competitor gaps, and search intent before scripting.
    """
    system_msg = (
        "You are an expert YouTube Trend Researcher. Analyze the user's topic and generate a structured research report.\n"
        "You MUST respond with a single valid JSON object containing these keys:\n"
        "{\n"
        "  \"primary_keyword\": \"string\",\n"
        "  \"competitor_gaps\": [\"gap1\", \"gap2\"],\n"
        "  \"cpm_tier\": \"high\" | \"medium\" | \"low\",\n"
        "  \"search_intent\": \"string\",\n"
        "  \"suggested_angles\": [\"angle1\", \"angle2\"]\n"
        "}\n"
        "Return ONLY the raw JSON without any explanations."
    )
    messages = [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": f"Analyze topic: {topic_prompt}"}
    ]
    res = await dispatch_llm_task("topic_research", messages, temperature=0.3)
    if not res.get("ok"):
        raise RuntimeError(f"topic_research LLM task failed: {res.get('error')}")
    data = safe_parse_json(res["content"])
    if not isinstance(data, dict):
        data = {}
    return {
        "primary_keyword": str(data.get("primary_keyword", topic_prompt)),
        "competitor_gaps": [str(x) for x in data.get("competitor_gaps", [])] if isinstance(data.get("competitor_gaps"), list) else [],
        "cpm_tier": str(data.get("cpm_tier", "medium")).lower(),
        "search_intent": str(data.get("search_intent", "")),
        "suggested_angles": [str(x) for x in data.get("suggested_angles", [])] if isinstance(data.get("suggested_angles"), list) else []
    }


async def run_script_outline(topic_research: Dict[str, Any], tone: str, persona: str) -> Dict[str, Any]:
    """
    LAVA Task: script_outline
    Turn the chosen angle into a structured script with <SHORT_BREAK>, <VISUAL_BREAK> markers.
    """
    system_msg = (
        "You are a professional video script writer.\n"
        "Create a script (approx 5-8 minutes read-time) based on the provided topic research.\n"
        "Format guidelines:\n"
        "1. Write the script in Markdown format.\n"
        "2. Break down the script into segments.\n"
        "3. Between each logical short segment (approx 40-60 seconds), insert '<SHORT_BREAK>' on its own line.\n"
        "4. Within each segment, place '<VISUAL_BREAK: brief visual description>' where a visual scene change should occur.\n"
        "5. Include a specific <RISK_DISCLOSURE> block at the end if the topic is finance/health.\n"
        "You MUST respond with a single valid JSON object containing these keys:\n"
        "{\n"
        "  \"title\": \"string\",\n"
        "  \"script_markdown\": \"string\",\n"
        "  \"tone_summary\": \"string\"\n"
        "}\n"
        "Return ONLY the raw JSON without any explanations."
    )
    user_content = f"Research data: {json.dumps(topic_research)}\nTone: {tone}\nPersona: {persona}"
    messages = [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": user_content}
    ]
    res = await dispatch_llm_task("script_outline", messages, temperature=0.5)
    if not res.get("ok"):
        raise RuntimeError(f"script_outline LLM task failed: {res.get('error')}")
    data = safe_parse_json(res["content"])
    if not isinstance(data, dict):
        data = {}
    return {
        "title": str(data.get("title", "")),
        "script_markdown": str(data.get("script_markdown", "")),
        "tone_summary": str(data.get("tone_summary", ""))
    }


async def run_visual_bible(title: str, genre: str, persona: str) -> Dict[str, Any]:
    """
    LAVA Task: visual_bible
    Create the project visual style, character continuity, and negative prompt library.
    """
    system_msg = (
        "You are a visual director. Set up a Style Guide and a Host/Character Profile for this video project.\n"
        "You MUST respond with a single valid JSON object matching the following schema:\n"
        "{\n"
        "  \"style_guide\": {\n"
        "    \"style_name\": \"string\",\n"
        "    \"aspect_ratio\": \"16:9\",\n"
        "    \"lighting\": \"string\",\n"
        "    \"color_grade\": \"string\",\n"
        "    \"typography\": \"string\",\n"
        "    \"negative_prompts\": [\"string\"]\n"
        "  },\n"
        "  \"characters\": [\n"
        "    {\n"
        "      \"character_id\": \"string\",\n"
        "      \"role\": \"string\",\n"
        "      \"description\": \"string\",\n"
        "      \"continuity_notes\": [\"string\"]\n"
        "    }\n"
        "  ]\n"
        "}\n"
        "Return ONLY the raw JSON."
    )
    user_content = f"Video Title: {title}\nGenre: {genre}\nHost Persona: {persona}"
    messages = [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": user_content}
    ]
    res = await dispatch_llm_task("visual_bible", messages, temperature=0.2)
    if not res.get("ok"):
        raise RuntimeError(f"visual_bible LLM task failed: {res.get('error')}")
    data = safe_parse_json(res["content"])
    if not isinstance(data, dict):
        data = {}
    
    style_guide = data.get("style_guide", {})
    if not isinstance(style_guide, dict):
        style_guide = {}
        
    characters = data.get("characters", [])
    if not isinstance(characters, list):
        characters = [characters] if isinstance(characters, dict) else []
        
    validated_characters = []
    for char in characters:
        if isinstance(char, dict):
            validated_characters.append({
                "character_id": str(char.get("character_id", "host_01")),
                "role": str(char.get("role", "host")),
                "description": str(char.get("description", "A standard speaker")),
                "continuity_notes": [str(x) for x in char.get("continuity_notes", [])] if isinstance(char.get("continuity_notes"), list) else []
            })
            
    return {
        "style_guide": {
            "style_name": str(style_guide.get("style_name", "documentary explainer")),
            "aspect_ratio": str(style_guide.get("aspect_ratio", "16:9")),
            "lighting": str(style_guide.get("lighting", "clean soft contrast")),
            "color_grade": str(style_guide.get("color_grade", "natural contrast")),
            "typography": str(style_guide.get("typography", "large readable sans-serif captions")),
            "negative_prompts": [str(x) for x in style_guide.get("negative_prompts", [])] if isinstance(style_guide.get("negative_prompts"), list) else []
        },
        "characters": validated_characters
    }


async def run_storyboard_candidates(
    cue_id: str,
    voice_text: str,
    visual_bible: Dict[str, Any],
    candidate_count: int = 3
) -> List[Dict[str, Any]]:
    """
    LAVA Task: storyboard (Generates multiple candidates for a single cue)
    Convert transcript cue into candidate shot specs.
    We ask the LLM to output N different creative visual options (e.g. Generated Image vs B-roll vs Screencast).
    """
    system_msg = (
        "You are a storyboard artist and prompt engineer.\n"
        "Convert the given spoken cue text into a list of candidate visual shot specs (exactly 3 options).\n"
        "For each option, output:\n"
        "- shot_type: 'talking_head' | 'generated_image' | 'broll' | 'screencast' | 'title_card'\n"
        "- prompt: Detailed visual prompt (incorporate style guide instructions. For 'generated_image', write a rich descriptive prompt. For 'screencast', describe the UI action. For 'broll', describe the stock video footage)\n"
        "- camera: Camera placement, movement, and lens description.\n"
        "- composition: Subject placement, negative space, safe zones for subtitles.\n"
        "- continuity: Color and aesthetic matching guidelines.\n"
        "- negative_prompt: Comma-separated negative prompt (e.g. blurry, text, bad anatomy, deformed)\n"
        "- quality_checks: List of 2-3 specific checklist items to verify this visual shot.\n"
        "\n"
        "You MUST respond with a single valid JSON object containing these keys:\n"
        "{\n"
        "  \"candidates\": [\n"
        "     {\n"
        "       \"shot_type\": \"string\",\n"
        "       \"prompt\": \"string\",\n"
        "       \"camera\": \"string\",\n"
        "       \"composition\": \"string\",\n"
        "       \"continuity\": \"string\",\n"
        "       \"negative_prompt\": \"string\",\n"
        "       \"quality_checks\": [\"string\"]\n"
        "     }\n"
        "  ]\n"
        "}\n"
        "Return ONLY the raw JSON."
    )
    user_content = (
        f"Cue ID: {cue_id}\n"
        f"Voice text: \"{voice_text}\"\n"
        f"Visual style: {json.dumps(visual_bible.get('style_guide', {}))}\n"
        f"Character info: {json.dumps(visual_bible.get('characters', []))}"
    )
    messages = [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": user_content}
    ]
    res = await dispatch_llm_task("storyboard", messages, temperature=0.6)
    if not res.get("ok"):
        raise RuntimeError(f"storyboard LLM task failed: {res.get('error')}")
    data = safe_parse_json(res["content"])
    if not isinstance(data, dict):
        data = {}
        
    candidates = data.get("candidates", [])
    if not isinstance(candidates, list):
        candidates = [candidates] if isinstance(candidates, dict) else []
    
    validated_candidates = []
    for c in candidates:
        if isinstance(c, dict):
            validated_candidates.append({
                "cue_id": cue_id,
                "shot_type": str(c.get("shot_type", "generated_image")),
                "prompt": str(c.get("prompt", "")),
                "camera": str(c.get("camera", "")),
                "composition": str(c.get("composition", "")),
                "continuity": str(c.get("continuity", "")),
                "negative_prompt": str(c.get("negative_prompt", "")),
                "quality_checks": [str(x) for x in c.get("quality_checks", [])] if isinstance(c.get("quality_checks"), list) else []
            })
    return validated_candidates


async def run_quality_review(
    cue_id: str,
    voice_text: str,
    candidate: Dict[str, Any],
    visual_bible: Dict[str, Any]
) -> Dict[str, Any]:
    """
    LAVA Task: quality_review
    Review a candidate shot spec against visual bible, continuity, and platform quality gates.
    Outputs a score (0-100) and findings list.
    """
    system_msg = (
        "You are an AI Video Quality Assurance Director.\n"
        "Evaluate the proposed shot spec against the style guide and spoken voice cue.\n"
        "Calculate a score from 0 to 100 based on:\n"
        "- Continuity matching (does it fit host profile and style guide color palette?)\n"
        "- Safe zone (is there room for captions?)\n"
        "- Visual clarity (does the prompt avoid messy text, watermarks, bad hands?)\n"
        "- Intent relevance (does the visual match the spoken script?)\n"
        "\n"
        "You MUST respond with a single valid JSON object containing these keys:\n"
        "{\n"
        "  \"score\": 85, (integer 0-100)\n"
        "  \"findings\": [\n"
        "     {\n"
        "       \"code\": \"string (e.g. unsafe_zone, style_mismatch, text_artifact)\",\n"
        "       \"severity\": \"blocker\" | \"warning\",\n"
        "       \"message\": \"Detailed description of the quality issue\"\n"
        "     }\n"
        "  ]\n"
        "}\n"
        "Return ONLY the raw JSON."
    )
    user_content = (
        f"Cue ID: {cue_id}\n"
        f"Voice text: \"{voice_text}\"\n"
        f"Proposed Candidate: {json.dumps(candidate)}\n"
        f"Visual style: {json.dumps(visual_bible.get('style_guide', {}))}"
    )
    messages = [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": user_content}
    ]
    res = await dispatch_llm_task("quality_review", messages, temperature=0.2)
    if not res.get("ok"):
        raise RuntimeError(f"quality_review LLM task failed: {res.get('error')}")
    data = safe_parse_json(res["content"])
    if not isinstance(data, dict):
        data = {}
    
    try:
        score = int(data.get("score", 70))
    except (ValueError, TypeError):
        score = 70
        
    findings = data.get("findings", [])
    if not isinstance(findings, list):
        findings = [findings] if isinstance(findings, dict) else []
        
    validated_findings = []
    for f in findings:
        if isinstance(f, dict):
            validated_findings.append({
                "cue_id": cue_id,
                "code": str(f.get("code", "quality_issue")),
                "severity": str(f.get("severity", "warning")),
                "message": str(f.get("message", "Issues found during AI review"))
            })
            
    return {
        "score": score,
        "findings": validated_findings
    }


async def run_packaging(script_markdown: str, topic_research: Dict[str, Any]) -> Dict[str, Any]:
    """
    LAVA Task: packaging
    Generate title ideas, thumbnail prompt, SEO description, and affiliate disclosures.
    """
    system_msg = (
        "You are an expert YouTube Growth & SEO optimizer.\n"
        "Generate packaging assets based on the script.\n"
        "You MUST respond with a single valid JSON object containing these keys:\n"
        "{\n"
        "  \"candidate_titles\": [\"title1\", \"title2\", \"title3\"],\n"
        "  \"selected_title\": \"string (the best SEO title)\",\n"
        "  \"thumbnail_prompt\": \"string (detailed descriptive prompt for DALL-E 3/SDXL)\",\n"
        "  \"description\": \"string (YouTube description including first two lines, chapters, and disclosures)\",\n"
        "  \"tags\": [\"tag1\", \"tag2\"]\n"
        "}\n"
        "Return ONLY the raw JSON."
    )
    user_content = f"Script markdown: {script_markdown[:8000]}\nResearch data: {json.dumps(topic_research)}"
    messages = [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": user_content}
    ]
    res = await dispatch_llm_task("packaging", messages, temperature=0.4)
    if not res.get("ok"):
        raise RuntimeError(f"packaging LLM task failed: {res.get('error')}")
    data = safe_parse_json(res["content"])
    if not isinstance(data, dict):
        data = {}
        
    return {
        "candidate_titles": [str(x) for x in data.get("candidate_titles", [])] if isinstance(data.get("candidate_titles"), list) else [],
        "selected_title": str(data.get("selected_title", "")),
        "thumbnail_prompt": str(data.get("thumbnail_prompt", "")),
        "description": str(data.get("description", "")),
        "tags": [str(x) for x in data.get("tags", [])] if isinstance(data.get("tags"), list) else []
    }
