import os
import shutil
import subprocess
import logging
from pathlib import Path
from pipeline.models.cue_ledger import CueLedger

# Try to import Pillow for burn-in subtitle overlay on frames
try:
    from PIL import Image, ImageDraw, ImageFont
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

logger = logging.getLogger("FFmpeg_Renderer")
logger.setLevel(logging.INFO)


def overlay_subtitle_pillow(image_path: Path, text: str, output_path: Path) -> bool:
    """Overlay a styled subtitle onto an image frame using Pillow."""
    if not HAS_PIL:
        return False
    try:
        with Image.open(str(image_path)) as img:
            # Convert to RGB to ensure saving as standard PNG/JPEG
            img = img.convert("RGB")
            draw = ImageDraw.Draw(img)
            
            width, height = img.size
            # We want subtitle at the bottom 15% of the frame
            # Draw semi-transparent background bar
            bar_height = 80
            bar_y = height - bar_height - 30
            
            # Semi-transparent black rectangle: overlay color (10, 10, 10, 180)
            overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
            overlay_draw = ImageDraw.Draw(overlay)
            overlay_draw.rectangle([(50, bar_y), (width - 50, bar_y + bar_height)], fill=(0, 0, 0, 170))
            img = Image.alpha_composite(img.convert('RGBA'), overlay).convert('RGB')
            draw = ImageDraw.Draw(img)
            
            # Draw text
            # Wrap subtitle text
            words = text.split()
            lines = []
            current_line = []
            for word in words:
                if len(" ".join(current_line + [word])) < 50:
                    current_line.append(word)
                else:
                    lines.append(" ".join(current_line))
                    current_line = [word]
            if current_line:
                lines.append(" ".join(current_line))
            
            text_to_draw = "\n".join(lines[:2])
            
            # Simple centered drawing (since we don't assume custom TrueType font is installed, 
            # we use default font but draw multiple offsets for a tiny shadow effect if needed)
            # Center of text block
            draw.text((width // 2 - len(text_to_draw)*3, bar_y + 20), text_to_draw, fill=(255, 255, 255))
            
            img.save(str(output_path), "PNG")
            return True
    except Exception as e:
        logger.error(f"Failed to overlay subtitle via Pillow: {e}")
        return False


def generate_temp_subtitle_frames(project_dir: Path, cue_ledger: CueLedger) -> list[tuple[Path, float]]:
    """
    Generate subtitle-injected temporary frames for every cue.
    Returns a list of tuples containing (frame_path, duration_sec).
    """
    temp_dir = project_dir / "temp_frames"
    temp_dir.mkdir(exist_ok=True)
    
    frame_list = []
    
    for cue in cue_ledger.cues:
        cue_id = cue.cue_id
        duration_sec = max(0.5, (cue.end_ms - cue.start_ms) / 1000.0)
        
        # Look for existing generated image asset
        asset_path = project_dir / "assets" / f"asset_{cue_id}.png"
        
        # If it doesn't exist, look in fallback assets directory or generate one
        if not asset_path.exists():
            # Check if there is a general placeholder in assets
            asset_path = project_dir / f"asset_{cue_id}.png"
            
        temp_frame_path = temp_dir / f"temp_{cue_id}.png"
        
        subtitle_text = cue.subtitle_text or cue.voice_text or ""
        
        if asset_path.exists():
            # Inject subtitle into the AI-generated frame
            success = overlay_subtitle_pillow(asset_path, subtitle_text, temp_frame_path)
            if not success:
                shutil.copy(str(asset_path), str(temp_frame_path))
        else:
            # Draw on a plain dark slate background
            if HAS_PIL:
                with Image.new("RGB", (1280, 720), color=(17, 24, 39)) as img: # Tailwind gray-900
                    draw = ImageDraw.Draw(img)
                    # Text wrapping
                    words = subtitle_text.split()
                    lines = []
                    current = []
                    for w in words:
                        if len(" ".join(current + [w])) < 45:
                            current.append(w)
                        else:
                            lines.append(" ".join(current))
                            current = [w]
                    if current:
                        lines.append(" ".join(current))
                    
                    y = 300
                    for line in lines[:5]:
                        draw.text((150, y), line, fill=(243, 244, 246)) # gray-50
                        y += 40
                    img.save(str(temp_frame_path), "PNG")
            else:
                # Tiny dummy PNG
                dummy_png = (
                    b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4'
                    b'\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82'
                )
                temp_frame_path.write_bytes(dummy_png)
                
        frame_list.append((temp_frame_path, duration_sec))
        
    return frame_list


def render_placeholder_preview(output_path: Path, cue_ledger: CueLedger, title: str) -> Path | None:
    ffmpeg = shutil.which("ffmpeg")
    project_dir = output_path.parent
    
    if ffmpeg is None:
        manifest_path = output_path.with_suffix(".ffmpeg.txt")
        manifest_path.write_text("ffmpeg not found; preview rendering skipped.\n", encoding="utf-8")
        return None

    # Determine if a voiceover audio file exists
    voiceover_path = project_dir / "voiceover.wav"
    if not voiceover_path.exists():
        # Look for backup mp3
        voiceover_path = project_dir / "voiceover.mp3"
        
    has_audio = voiceover_path.exists()
    
    # 1. Generate subtitle-injected frames for all cues
    frames = generate_temp_subtitle_frames(project_dir, cue_ledger)
    
    if not frames:
        logger.warning("No frames generated. Falling back to default solid color preview.")
        return None
        
    # 2. Write the FFmpeg concat demuxer file
    concat_file = project_dir / "ffmpeg_concat.txt"
    with open(concat_file, "w", encoding="utf-8") as f:
        for frame_path, duration in frames:
            # Replace backslashes for FFmpeg on Windows
            safe_path = str(frame_path.resolve()).replace("\\", "/")
            f.write(f"file '{safe_path}'\n")
            f.write(f"duration {duration}\n")
        # Concat demuxer requirement: duplicate the last file at the end
        if frames:
            safe_path = str(frames[-1][0].resolve()).replace("\\", "/")
            f.write(f"file '{safe_path}'\n")

    # 3. Assemble the FFmpeg command with crossfade transitions
    # Use concat demuxer for frame assembly, then apply fade filter for polish
    cmd = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file)]
    
    if has_audio:
        cmd.extend(["-i", str(voiceover_path)])
    
    # Calculate total duration for fade-out
    total_duration_sec = sum(d for _, d in frames)
    fade_out_start = max(0, total_duration_sec - 1.0)
    
    # Build video filter: fade-in at start, fade-out at end
    vf_parts = [
        f"fade=t=in:st=0:d=0.5",
        f"fade=t=out:st={fade_out_start:.2f}:d=1.0",
        "format=yuv420p",
    ]
    video_filter = ",".join(vf_parts)
    
    cmd.extend(["-vf", video_filter])
    
    if has_audio:
        # Audio filter: fade-in and fade-out for smooth audio transitions
        af_parts = [
            "afade=t=in:st=0:d=0.3",
            f"afade=t=out:st={fade_out_start:.2f}:d=1.0",
        ]
        cmd.extend(["-af", ",".join(af_parts)])
        # Encode video with h264, audio with AAC, cut to shortest input
        cmd.extend(["-c:v", "libx264", "-preset", "fast", "-crf", "23", "-c:a", "aac", "-b:a", "128k", "-shortest"])
    else:
        # Encode silent video
        cmd.extend(["-c:v", "libx264", "-preset", "fast", "-crf", "23"])
        
    cmd.append(str(output_path))
    
    command_log_path = output_path.with_suffix(".ffmpeg.txt")
    command_log_path.write_text(" ".join(cmd), encoding="utf-8")

    try:
        logger.info(f"Running FFmpeg render: {' '.join(cmd)}")
        res = subprocess.run(cmd, check=True, capture_output=True, text=True)
        logger.info("FFmpeg rendering completed successfully.")
    except subprocess.CalledProcessError as exc:
        logger.error(f"FFmpeg render failed with exit status {exc.returncode}")
        logger.error(f"FFmpeg stderr: {exc.stderr}")
        logger.error(f"FFmpeg stdout: {exc.stdout}")
        error_log = f"\n\nERROR: {exc}\nSTDERR:\n{exc.stderr}\nSTDOUT:\n{exc.stdout}\n"
        command_log_path.write_text(command_log_path.read_text(encoding="utf-8") + error_log, encoding="utf-8")
        return None
    except Exception as exc:
        logger.error(f"FFmpeg render failed: {exc}")
        command_log_path.write_text(command_log_path.read_text(encoding="utf-8") + f"\n\nERROR: {exc}\n", encoding="utf-8")
        return None
    finally:
        temp_dir = project_dir / "temp_frames"
        if temp_dir.exists():
            try:
                shutil.rmtree(temp_dir)
            except Exception as e:
                logger.warning(f"Could not clean up temp_frames directory: {e}")
        if concat_file.exists():
            try:
                concat_file.unlink()
            except Exception as e:
                logger.warning(f"Could not clean up concat_file: {e}")

    return output_path
