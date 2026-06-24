import os
import httpx
import logging
import asyncio
import hashlib
import shutil
import base64
from pathlib import Path
from pipeline.secrets import load_runtime_secrets

# We try to import Pillow (PIL) to generate clean placeholders.
# If not available, we write a small mock byte stream or log warning.
try:
    from PIL import Image, ImageDraw, ImageFont
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

logger = logging.getLogger("Image_Generator")
logger.setLevel(logging.INFO)


def generate_placeholder_image(output_path: Path, text: str) -> None:
    """Generate a clean colored placeholder PNG image with descriptive text using Pillow."""
    if not HAS_PIL:
        # Fallback to writing a tiny dummy 1x1 PNG file if PIL is not installed
        dummy_png = (
            b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4'
            b'\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82'
        )
        output_path.write_bytes(dummy_png)
        logger.info(f"Fallback: Written 1x1 dummy PNG to {output_path} (Pillow not installed)")
        return

    try:
        # Create a 1280x720 (16:9) image with a nice dark gradient or solid color
        img = Image.new("RGB", (1280, 720), color=(43, 43, 64))
        draw = ImageDraw.Draw(img)
        
        # Draw a subtle border
        draw.rectangle([(20, 20), (1260, 700)], outline=(100, 100, 150), width=3)
        
        # Write the text (centered)
        # We wrap long text to fit the image
        words = text.split()
        lines = []
        current_line = []
        for word in words:
            if len(" ".join(current_line + [word])) < 35:
                current_line.append(word)
            else:
                lines.append(" ".join(current_line))
                current_line = [word]
        if current_line:
            lines.append(" ".join(current_line))
            
        y_offset = 250
        for line in lines[:8]:
            draw.text((100, y_offset), line, fill=(240, 240, 255))
            y_offset += 50
            
        draw.text((100, 620), "[AI PIPELINE GENERATED PLACEHOLDER]", fill=(120, 120, 160))
        
        img.save(str(output_path), "PNG")
        logger.info(f"Generated placeholder image with text at {output_path}")
    except Exception as e:
        logger.error(f"Failed to generate Pillow placeholder: {e}")
        # write tiny dummy as ultimate fallback
        dummy_png = (
            b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4'
            b'\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82'
        )
        output_path.write_bytes(dummy_png)


def _is_retryable_error(status_code: int, err_msg: str) -> bool:
    err_msg_lower = err_msg.lower()
    return (
        status_code in {408, 429, 502, 503, 504}
        or "429" in err_msg
        or "too many requests" in err_msg_lower
        or "timeout" in err_msg_lower
        or "connection" in err_msg_lower
    )


async def generate_ai_image(prompt: str, output_path: Path, negative_prompt: str = "") -> Path:
    """
    Generate an AI image via OpenRouter SDXL API.
    Falls back to a clean Pillow placeholder image if API fails, is exhausted, or API key is missing.
    Includes image caching to optimize token usage and retry backoff.
    """
    load_runtime_secrets()
    api_key = os.getenv("OPENROUTER_API_KEY")

    if not api_key:
        logger.warning("OPENROUTER_API_KEY not found. Generating Pillow placeholder instead.")
        await asyncio.to_thread(generate_placeholder_image, output_path, prompt)
        return output_path

    # 1. Caching Check
    from pipeline.settings import get_settings
    try:
        settings = get_settings()
        cache_dir = settings.data_dir / "image_cache"
    except Exception:
        cache_dir = Path("./image_cache")

    cache_key = hashlib.sha256(f"{prompt}|||{negative_prompt}".encode("utf-8")).hexdigest()
    cache_file = cache_dir / f"{cache_key}.png"

    if cache_file.exists():
        logger.info(f"Cache hit: copying {cache_file.name} to {output_path}")
        try:
            await asyncio.to_thread(cache_file.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(shutil.copy2, cache_file, output_path)
            return output_path
        except Exception as ce:
            logger.warning(f"Failed to copy cached file: {ce}")

    logger.info(f"Generating image via OpenRouter SDXL for prompt: {prompt[:80]}...")

    # We use stable diffusion XL on OpenRouter
    url = "https://openrouter.ai/api/v1/images/generations"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "stabilityai/stable-diffusion-xl",
        "prompt": prompt,
        "negative_prompt": negative_prompt or "blurry, low resolution, bad hands",
        "width": 1024,
        "height": 576,  # 16:9 ratio
        "response_format": "b64_json"
    }

    max_retries = 3
    delay = 2.0
    for attempt in range(max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=90) as client:
                response = await client.post(url, json=payload, headers=headers)

                # Check for 402 Payment Required (Balance Exhausted)
                if response.status_code == 402 or "402" in response.text:
                    logger.error("OpenRouter balance exhausted (402 Payment Required).")
                    raise RuntimeError("OpenRouter API 402: Payment Required / Balance Exhausted")

                if response.status_code != 200:
                    err_text = response.text
                    if _is_retryable_error(response.status_code, err_text) and attempt < max_retries:
                        logger.warning(f"OpenRouter rate limit or temp error (status {response.status_code}). Retrying in {delay}s (attempt {attempt + 1}/{max_retries})...")
                        await asyncio.sleep(delay)
                        delay *= 2.0
                        continue
                    else:
                        raise RuntimeError(f"OpenRouter Image Gen Error (status {response.status_code}): {err_text}")

                data = response.json()
                if "error" in data:
                    err_info = data["error"]
                    err_msg = err_info.get("message", str(err_info)) if isinstance(err_info, dict) else str(err_info)
                    raise RuntimeError(f"OpenRouter API Error: {err_msg}")

                if "data" not in data or not data["data"]:
                    raise RuntimeError(f"OpenRouter returned no image data: {data}")

                b64_data = data["data"][0].get("b64_json")
                if not b64_data:
                    raise RuntimeError(f"OpenRouter returned data but no b64_json: {data}")

                image_data = base64.b64decode(b64_data)
                await asyncio.to_thread(output_path.write_bytes, image_data)
                logger.info(f"Successfully generated and downloaded AI image to {output_path}")

                # Save to cache
                try:
                    await asyncio.to_thread(cache_dir.mkdir, parents=True, exist_ok=True)
                    await asyncio.to_thread(shutil.copy2, output_path, cache_file)
                    logger.info(f"Successfully cached generated image to {cache_file}")
                except Exception as ce:
                    logger.warning(f"Failed to cache generated image: {ce}")

                return output_path

        except Exception as e:
            if "402" in str(e) or attempt == max_retries:
                logger.error(f"Image generation failed: {e}. Falling back to Pillow placeholder.")
                await asyncio.to_thread(generate_placeholder_image, output_path, prompt)
                return output_path

            # Check if exception is retryable (e.g. httpx connection error)
            err_msg = str(e)
            if _is_retryable_error(500, err_msg) and attempt < max_retries:
                logger.warning(f"Retryable connection error: {err_msg}. Retrying in {delay}s...")
                await asyncio.sleep(delay)
                delay *= 2.0
            else:
                logger.error(f"Image generation failed: {e}. Falling back to Pillow placeholder.")
                await asyncio.to_thread(generate_placeholder_image, output_path, prompt)
                return output_path
