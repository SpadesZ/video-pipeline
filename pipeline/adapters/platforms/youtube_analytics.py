# 檔案路徑: video-pipeline/pipeline/adapters/platforms/youtube_analytics.py
# 產生時間: 2026-06-25 14:33 +08:00
# 版本: v1.0
# 模組定位:
#   YouTube Analytics API 適配器，用於取得影片績效數據。
# 主要責任:
#   1. 透過 OAuth 2.0 取得 YouTube Data API v3 與 YouTube Analytics API 的授權服務。
#   2. 擷取單支影片的觀看數、點閱率 (CTR)、平均觀看時長、留存曲線、RPM 等指標。
#   3. 列出頻道近期上傳的影片清單。
#   4. 安全地保存與載入 OAuth 令牌至 secrets/ 目錄。
# 綠標提醒:
#   - 需安裝 google-api-python-client 與 google-auth-oauthlib 套件。
#   - 若套件未安裝，模組會以 ImportError 攔截並記錄警告，所有公開函式回傳空值。
#   - OAuth 令牌檔案預設位於 secrets/youtube_oauth_token.json，勿納入版控。
# --------------------------------------------------------------------------

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pipeline.secrets import load_runtime_secrets

logger = logging.getLogger("YouTube_Analytics")
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# 選擇性匯入 Google API 相關套件
# ---------------------------------------------------------------------------
try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import Resource, build

    HAS_GOOGLE_API = True
except ImportError:
    HAS_GOOGLE_API = False
    # 提供佔位型別，避免型別提示在未安裝時報錯
    Resource = Any  # type: ignore[assignment,misc]
    Credentials = Any  # type: ignore[assignment,misc]
    logger.warning(
        "google-api-python-client / google-auth-oauthlib 未安裝。"
        "YouTube Analytics 功能將無法使用。"
        "請執行: pip install google-api-python-client google-auth-oauthlib"
    )

# OAuth 2.0 授權範圍
_SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]

_TOKEN_FILENAME = "youtube_oauth_token.json"


# ============================= 令牌管理 ====================================


def save_oauth_tokens(credentials: Credentials, secrets_dir: Path) -> None:
    """將 OAuth 憑證持久化至 secrets/ 目錄下的 JSON 檔案。

    Parameters
    ----------
    credentials:
        已取得授權的 google.oauth2.credentials.Credentials 物件。
    secrets_dir:
        專案的 secrets/ 目錄路徑。
    """
    if not HAS_GOOGLE_API:
        logger.error("Google API 套件未安裝，無法儲存 OAuth 令牌。")
        return

    secrets_dir.mkdir(parents=True, exist_ok=True)
    token_path = secrets_dir / _TOKEN_FILENAME

    token_data: dict[str, Any] = {
        "token": credentials.token,
        "refresh_token": credentials.refresh_token,
        "token_uri": credentials.token_uri,
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "scopes": list(credentials.scopes) if credentials.scopes else _SCOPES,
    }
    if credentials.expiry:
        token_data["expiry"] = credentials.expiry.isoformat()

    token_path.write_text(
        json.dumps(token_data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    logger.info(f"OAuth 令牌已儲存至: {token_path}")


def load_oauth_tokens(secrets_dir: Path) -> Credentials | None:
    """從 secrets/ 目錄載入已保存的 OAuth 令牌。

    Returns
    -------
    Credentials | None
        成功載入時回傳 Credentials 物件；檔案不存在或格式錯誤時回傳 None。
    """
    if not HAS_GOOGLE_API:
        logger.warning("Google API 套件未安裝，無法載入 OAuth 令牌。")
        return None

    token_path = secrets_dir / _TOKEN_FILENAME
    if not token_path.exists():
        logger.info(f"OAuth 令牌檔案不存在: {token_path}")
        return None

    try:
        token_data = json.loads(token_path.read_text(encoding="utf-8"))
        creds = Credentials(
            token=token_data["token"],
            refresh_token=token_data.get("refresh_token"),
            token_uri=token_data.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=token_data.get("client_id"),
            client_secret=token_data.get("client_secret"),
            scopes=token_data.get("scopes", _SCOPES),
        )
        # 還原過期時間
        expiry_str = token_data.get("expiry")
        if expiry_str:
            creds.expiry = datetime.fromisoformat(expiry_str)
        logger.info("已成功載入已保存的 OAuth 令牌。")
        return creds
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        logger.error(f"載入 OAuth 令牌失敗: {exc}")
        return None


# ========================== 服務建立 =======================================


def _refresh_or_authorize(secrets_dir: Path) -> Credentials | None:
    """嘗試載入既有令牌並刷新；若不存在則走 OAuth 授權流程。

    環境變數需求:
        YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET
    """
    load_runtime_secrets()

    # 1. 嘗試載入已保存的令牌
    creds = load_oauth_tokens(secrets_dir)

    if creds and creds.valid:
        return creds

    # 2. 令牌存在但過期 → 嘗試刷新
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            save_oauth_tokens(creds, secrets_dir)
            logger.info("OAuth 令牌已透過 refresh_token 成功刷新。")
            return creds
        except Exception as exc:
            logger.warning(f"刷新 OAuth 令牌失敗: {exc}，將重新執行授權流程。")

    # 3. 無有效令牌 → 使用環境變數中的 client_id/secret 啟動授權
    client_id = os.getenv("YOUTUBE_CLIENT_ID")
    client_secret = os.getenv("YOUTUBE_CLIENT_SECRET")
    if not client_id or not client_secret:
        logger.error(
            "未設定 YOUTUBE_CLIENT_ID 或 YOUTUBE_CLIENT_SECRET 環境變數，"
            "無法建立 OAuth 授權。"
        )
        return None

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }

    try:
        flow = InstalledAppFlow.from_client_config(client_config, scopes=_SCOPES)
        creds = flow.run_local_server(port=0, open_browser=True)
        save_oauth_tokens(creds, secrets_dir)
        logger.info("OAuth 授權流程完成，令牌已保存。")
        return creds
    except Exception as exc:
        logger.error(f"OAuth 授權流程失敗: {exc}")
        return None


def get_youtube_service(
    secrets_dir: Path,
) -> tuple[Resource, Resource] | None:
    """建立 YouTube Data API v3 與 YouTube Analytics API 的服務物件。

    Parameters
    ----------
    secrets_dir:
        專案的 secrets/ 目錄路徑，用於存取 OAuth 令牌。

    Returns
    -------
    tuple[Resource, Resource] | None
        回傳 (youtube_data_service, youtube_analytics_service)；
        若套件未安裝或授權失敗則回傳 None。
    """
    if not HAS_GOOGLE_API:
        logger.warning("Google API 套件未安裝，無法建立 YouTube 服務。")
        return None

    creds = _refresh_or_authorize(secrets_dir)
    if creds is None:
        return None

    try:
        youtube_data = build("youtube", "v3", credentials=creds)
        youtube_analytics = build("youtubeAnalytics", "v2", credentials=creds)
        logger.info("YouTube Data API v3 與 YouTube Analytics API 服務已建立。")
        return youtube_data, youtube_analytics
    except Exception as exc:
        logger.error(f"建立 YouTube API 服務失敗: {exc}")
        return None


# ========================== 指標擷取 =======================================


def fetch_video_metrics(video_id: str, secrets_dir: Path) -> dict | None:
    """擷取指定影片的績效指標。

    回傳欄位:
        - views: 觀看次數
        - impressions_ctr: 曝光點閱率 (%)
        - average_view_duration_seconds: 平均觀看秒數
        - estimated_rpm: 每千次曝光收益 (美元)
        - retention_curve: 留存率列表 (每 10% 區間)
        - title: 影片標題
        - published_at: 發佈時間

    Parameters
    ----------
    video_id:
        YouTube 影片 ID (如 ``dQw4w9WgXcQ``)。
    secrets_dir:
        專案的 secrets/ 目錄路徑。

    Returns
    -------
    dict | None
        指標字典；若 API 不可用或查詢失敗則回傳 None。
    """
    if not HAS_GOOGLE_API:
        logger.warning("Google API 套件未安裝，無法取得影片指標。")
        return None

    services = get_youtube_service(secrets_dir)
    if services is None:
        return None

    youtube_data, youtube_analytics = services
    result: dict[str, Any] = {"video_id": video_id}

    # ------------------------------------------------------------------
    # 1. 從 YouTube Data API 取得影片基本資訊與統計
    # ------------------------------------------------------------------
    try:
        video_response = (
            youtube_data.videos()
            .list(part="snippet,statistics,contentDetails", id=video_id)
            .execute()
        )
        items = video_response.get("items", [])
        if not items:
            logger.warning(f"找不到影片: {video_id}")
            return None

        snippet = items[0]["snippet"]
        statistics = items[0].get("statistics", {})
        result["title"] = snippet.get("title", "")
        result["published_at"] = snippet.get("publishedAt", "")
        result["views"] = int(statistics.get("viewCount", 0))
        result["likes"] = int(statistics.get("likeCount", 0))
        result["comments"] = int(statistics.get("commentCount", 0))
    except Exception as exc:
        logger.error(f"取得影片基本資訊失敗 (video_id={video_id}): {exc}")
        return None

    # ------------------------------------------------------------------
    # 2. 從 YouTube Analytics API 取得進階指標
    # ------------------------------------------------------------------
    published_at = result.get("published_at", "")
    if published_at:
        try:
            start_date = datetime.fromisoformat(
                published_at.replace("Z", "+00:00")
            ).strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            start_date = (datetime.utcnow() - timedelta(days=365)).strftime("%Y-%m-%d")
    else:
        start_date = (datetime.utcnow() - timedelta(days=365)).strftime("%Y-%m-%d")

    end_date = datetime.utcnow().strftime("%Y-%m-%d")

    try:
        analytics_response = (
            youtube_analytics.reports()
            .query(
                ids="channel==MINE",
                startDate=start_date,
                endDate=end_date,
                metrics="views,estimatedMinutesWatched,averageViewDuration,impressions,impressionClickThroughRate,estimatedRevenue",
                filters=f"video=={video_id}",
            )
            .execute()
        )
        rows = analytics_response.get("rows", [])
        if rows:
            row = rows[0]
            # 欄位順序對應 metrics 參數:
            # views, estimatedMinutesWatched, averageViewDuration,
            # impressions, impressionClickThroughRate, estimatedRevenue
            result["analytics_views"] = row[0]
            result["estimated_minutes_watched"] = row[1]
            result["average_view_duration_seconds"] = row[2]
            impressions = row[3]
            result["impressions"] = impressions
            result["impressions_ctr"] = round(row[4] * 100, 2)  # 轉為百分比
            estimated_revenue = row[5]
            result["estimated_revenue"] = round(estimated_revenue, 4)
            # RPM = (revenue / views) * 1000
            analytics_views = row[0]
            if analytics_views and analytics_views > 0:
                result["estimated_rpm"] = round(
                    (estimated_revenue / analytics_views) * 1000, 4
                )
            else:
                result["estimated_rpm"] = 0.0
        else:
            logger.info(f"影片 {video_id} 尚無 Analytics 資料。")
            result["analytics_views"] = 0
            result["estimated_minutes_watched"] = 0
            result["average_view_duration_seconds"] = 0
            result["impressions"] = 0
            result["impressions_ctr"] = 0.0
            result["estimated_rpm"] = 0.0
            result["estimated_revenue"] = 0.0
    except Exception as exc:
        logger.warning(f"取得影片 Analytics 指標失敗 (video_id={video_id}): {exc}")
        result["analytics_views"] = None
        result["impressions_ctr"] = None
        result["average_view_duration_seconds"] = None
        result["estimated_rpm"] = None
        result["estimated_revenue"] = None

    # ------------------------------------------------------------------
    # 3. 留存曲線 (Audience Retention)
    # ------------------------------------------------------------------
    try:
        retention_response = (
            youtube_analytics.reports()
            .query(
                ids="channel==MINE",
                startDate=start_date,
                endDate=end_date,
                metrics="audienceWatchRatio",
                dimensions="elapsedVideoTimeRatio",
                filters=f"video=={video_id}",
                sort="elapsedVideoTimeRatio",
            )
            .execute()
        )
        retention_rows = retention_response.get("rows", [])
        # 每一列: [elapsedVideoTimeRatio, audienceWatchRatio]
        retention_curve: list[dict[str, float]] = []
        for rrow in retention_rows:
            retention_curve.append(
                {
                    "elapsed_ratio": round(rrow[0], 4),
                    "watch_ratio": round(rrow[1], 4),
                }
            )
        result["retention_curve"] = retention_curve
    except Exception as exc:
        logger.warning(f"取得留存曲線失敗 (video_id={video_id}): {exc}")
        result["retention_curve"] = []

    logger.info(f"已成功取得影片指標: {video_id} ({result.get('title', '')})")
    return result


# ========================== 頻道影片列表 ====================================


def list_channel_videos(
    secrets_dir: Path, max_results: int = 50
) -> list[dict]:
    """列出已驗證頻道的近期上傳影片。

    Parameters
    ----------
    secrets_dir:
        專案的 secrets/ 目錄路徑。
    max_results:
        最多回傳的影片數量 (上限 50)。

    Returns
    -------
    list[dict]
        影片清單，每筆包含 video_id, title, published_at, thumbnail_url。
        若 API 不可用則回傳空列表。
    """
    if not HAS_GOOGLE_API:
        logger.warning("Google API 套件未安裝，無法列出頻道影片。")
        return []

    services = get_youtube_service(secrets_dir)
    if services is None:
        return []

    youtube_data, _ = services
    max_results = min(max(1, max_results), 50)

    try:
        # 取得頻道的上傳播放清單 ID
        channels_response = (
            youtube_data.channels()
            .list(part="contentDetails", mine=True)
            .execute()
        )
        channel_items = channels_response.get("items", [])
        if not channel_items:
            logger.warning("找不到已驗證的 YouTube 頻道。")
            return []

        uploads_playlist_id = (
            channel_items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
        )

        # 取得播放清單中的影片
        videos: list[dict] = []
        next_page_token: str | None = None

        while len(videos) < max_results:
            page_size = min(max_results - len(videos), 50)
            playlist_request = youtube_data.playlistItems().list(
                part="snippet",
                playlistId=uploads_playlist_id,
                maxResults=page_size,
                pageToken=next_page_token,
            )
            playlist_response = playlist_request.execute()

            for item in playlist_response.get("items", []):
                snippet = item["snippet"]
                resource_id = snippet.get("resourceId", {})
                video_id = resource_id.get("videoId", "")
                if not video_id:
                    continue

                thumbnails = snippet.get("thumbnails", {})
                thumbnail_url = ""
                for quality in ("maxres", "standard", "high", "medium", "default"):
                    if quality in thumbnails:
                        thumbnail_url = thumbnails[quality].get("url", "")
                        break

                videos.append(
                    {
                        "video_id": video_id,
                        "title": snippet.get("title", ""),
                        "published_at": snippet.get("publishedAt", ""),
                        "thumbnail_url": thumbnail_url,
                        "description": snippet.get("description", "")[:200],
                    }
                )

            next_page_token = playlist_response.get("nextPageToken")
            if not next_page_token:
                break

        logger.info(f"已列出 {len(videos)} 支頻道影片。")
        return videos

    except Exception as exc:
        logger.error(f"列出頻道影片失敗: {exc}")
        return []
