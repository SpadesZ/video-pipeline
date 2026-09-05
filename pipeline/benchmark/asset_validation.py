# 檔案路徑: video-pipeline/pipeline/benchmark/asset_validation.py
# 產生時間: 2026-09-05 +08:00
# 版本: v1.0
# 模組定位:
#   Benchmark 參考素材的實質驗證。
# 主要責任:
#   1. 驗證檔案可解碼且確實是影像。
#   2. 驗證首幀的畫面比例與最低解析度。
# 說明:
#   只檢查檔案大小大於零是不夠的。一個只有 PNG 檔頭加上隨機位元組的
#   檔案會通過大小檢查，卻在上傳平台時失敗；比例錯誤的首幀則會讓
#   四個平台各自裁切或補邊，結果無法比較。這些都必須在派工前擋下。
# --------------------------------------------------------------------------

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

# 9:16 的比例值。允許少量偏差以涵蓋 768x1344 這類常見尺寸。
VERTICAL_RATIO = 9 / 16
RATIO_TOLERANCE = 0.05
# 首幀短邊下限。低於此值的圖片放大後細節不足，會影響身份判讀。
MIN_SHORT_EDGE = 512


class ImageCheck(BaseModel):
    path: str
    ok: bool = False
    width: int | None = None
    height: int | None = None
    image_format: str | None = None
    problems: list[str] = []

    @property
    def ratio(self) -> float | None:
        if not self.width or not self.height:
            return None
        return self.width / self.height


def validate_image(
    path: Path | str,
    require_vertical: bool = True,
    min_short_edge: int = MIN_SHORT_EDGE,
) -> ImageCheck:
    """驗證單一素材檔。任何失敗都回傳問題描述，不拋出例外。"""
    file_path = Path(path)
    result = ImageCheck(path=str(file_path), problems=[])

    if not file_path.exists():
        result.problems.append("檔案不存在")
        return result
    if file_path.stat().st_size == 0:
        result.problems.append("檔案為空")
        return result

    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError:  # pragma: no cover - Pillow 為必要相依
        result.problems.append("Pillow 不可用，無法驗證影像")
        return result

    try:
        with Image.open(file_path) as image:
            image.verify()
        # verify() 之後檔案指標失效，需重新開啟才能讀尺寸
        with Image.open(file_path) as image:
            result.width, result.height = image.size
            result.image_format = image.format
    except UnidentifiedImageError:
        result.problems.append("無法辨識為影像檔")
        return result
    except Exception as error:  # noqa: BLE001 - 損毀檔案的例外種類不一
        result.problems.append(f"影像解碼失敗: {str(error)[:120]}")
        return result

    if not result.width or not result.height:
        result.problems.append("無法取得影像尺寸")
        return result

    short_edge = min(result.width, result.height)
    if short_edge < min_short_edge:
        result.problems.append(
            f"短邊 {short_edge}px 低於下限 {min_short_edge}px"
        )

    if require_vertical:
        ratio = result.width / result.height
        if abs(ratio - VERTICAL_RATIO) > RATIO_TOLERANCE:
            result.problems.append(
                f"比例 {result.width}:{result.height} "
                f"({ratio:.3f}) 不符 9:16 ({VERTICAL_RATIO:.3f})"
            )

    result.ok = not result.problems
    return result
