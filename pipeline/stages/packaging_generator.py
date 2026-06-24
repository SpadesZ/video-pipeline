from pathlib import Path

from pipeline.models.production_artifact import ProductionArtifact


def format_youtube_time(ms: int) -> str:
    total_seconds = ms // 1000
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def get_video_chapters(artifact: ProductionArtifact) -> str:
    """Generate YouTube-compatible video chapters from the cue ledger."""
    if not artifact.cue_ledger or not artifact.cue_ledger.cues:
        return ""
    
    chapters = []
    # YouTube requires the first chapter to start at 00:00
    chapters.append("00:00 - Introduction")
    last_chapter_sec = 0
    
    for cue in artifact.cue_ledger.cues:
        sec = cue.start_ms // 1000
        # Chapters must be at least 15 seconds apart per YouTube rules
        if sec >= last_chapter_sec + 15:
            words = cue.voice_text.strip().split()
            if words:
                # Take the first 4-5 words as chapter name
                snippet = " ".join(words[:4]).rstrip(".,!?;:，。！")
                if snippet:
                    chapters.append(f"{format_youtube_time(cue.start_ms)} - {snippet}")
                    last_chapter_sec = sec
                    
    if len(chapters) > 1:
        return "\n".join(chapters)
    return ""


def write_upload_package(project_dir: Path, artifact: ProductionArtifact) -> Path:
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / "upload_package.md"
    title = artifact.title
    
    chapters_text = get_video_chapters(artifact)
    
    pkg = artifact.video_packaging
    pkg_dict = {}

    if pkg and isinstance(pkg, dict):
        candidate_titles = pkg.get("candidate_titles", [title])
        selected_title = pkg.get("selected_title", title)
        thumbnail_prompt = pkg.get("thumbnail_prompt", "No prompt generated")
        description_draft = pkg.get("description", "No description generated")
        tags_list = pkg.get("tags", [])
        tags = ", ".join(tags_list)
        
        if chapters_text and "00:00" not in description_draft:
            description_draft = f"{description_draft}\n\n### Video Chapters / Timestamps\n{chapters_text}"
        
        pkg_dict = {
            "candidate_titles": candidate_titles,
            "selected_title": selected_title,
            "thumbnail_prompt": thumbnail_prompt,
            "description": description_draft,
            "tags": tags_list
        }
        
        description = [
            f"# {selected_title} (Optimized Packaging)",
            "",
            "## Selected Title",
            selected_title,
            "",
            "## Candidate Titles",
            *[f"- {t}" for t in candidate_titles],
            "",
            "## Thumbnail Prompt",
            thumbnail_prompt,
            "",
            "## Description Draft",
            description_draft,
            "",
            "## Tags",
            tags,
            "",
            "## Human Review Checklist",
            "- Title and thumbnail are accurate.",
            "- Every asset has commercial-rights evidence.",
            "- AI disclosure is added if needed.",
            "- Financial or income claims include risk disclosure.",
        ]
    else:
        description_draft = "A practical, source-aware breakdown. Add affiliate and AI disclosure text only after final human review."
        if chapters_text:
            description_draft = f"{description_draft}\n\n### Video Chapters / Timestamps\n{chapters_text}"
            
        pkg_dict = {
            "candidate_titles": [
                title,
                f"What Most People Miss About {title}",
                f"{title}: A Practical Breakdown"
            ],
            "selected_title": title,
            "thumbnail_prompt": "No prompt generated",
            "description": description_draft,
            "tags": [title.lower(), "tutorial", "breakdown"]
        }
        
        description = [
            f"# {title}",
            "",
            "## Candidate Titles",
            f"1. {pkg_dict['candidate_titles'][0]}",
            f"2. {pkg_dict['candidate_titles'][1]}",
            f"3. {pkg_dict['candidate_titles'][2]}",
            "",
            "## Description Draft",
            description_draft,
            "",
            "## Human Review Checklist",
            "- Title and thumbnail are accurate.",
            "- Every asset has commercial-rights evidence.",
            "- AI disclosure is added if needed.",
            "- Financial or income claims include risk disclosure.",
        ]
        
    path.write_text("\n".join(description), encoding="utf-8")
    
    # Write structured JSON representation alongside markdown
    from pipeline.utils.files import write_json
    json_path = project_dir / "upload_package.json"
    write_json(json_path, pkg_dict)
    
    return path

