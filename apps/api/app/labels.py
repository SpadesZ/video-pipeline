# 檔案路徑: video-pipeline/apps/api/app/labels.py
# 產生時間: 2026-09-06 +08:00
# 版本: v1.0
# 模組定位:
#   介面用語表。
# 主要責任:
#   1. 把內部代號與狀態碼換成看得懂的中文。
#   2. 讓同一個東西在每一頁的說法一致。
# 說明:
#   bm_a1_walk_slow_push、pending_manual、identity_consistency 這些是給
#   程式用的代號。直接顯示在畫面上，等於要求使用者先讀過原始碼才會操作。
#
#   對照表放在介面層而不是 v1_pack：鏡頭定義是內容真相，改動會影響
#   benchmark 結果；這裡只是換個說法給人看，兩者不該混在一起。
#
#   代號本身不刪除，改以次要文字保留，出問題時仍查得到是哪一筆。
# --------------------------------------------------------------------------

from __future__ import annotations

# 六顆固定鏡頭的白話名稱。前面的編號沿用情境代號，方便與素材對照。
SHOT_NAMES: dict[str, str] = {
    "bm_a1_walk_slow_push": "A1 · 雨夜天橋，緩慢推近",
    "bm_a2_closeup_expression": "A2 · 臉部特寫，表情變化",
    "bm_b1_two_shot_dialogue": "B1 · 便利商店雙人對話",
    "bm_b2_ots_on_a": "B2 · 過肩鏡頭，看向 A",
    "bm_b3_ots_on_b": "B3 · 過肩反打，看向 B",
    "bm_c1_run_tracking": "C1 · 雨中巷弄，奔跑跟拍",
}

# 情境代號 → 白話說法
SCENARIO_NAMES: dict[str, str] = {
    "single_character_cinematic": "單人運鏡",
    "two_character_dialogue": "雙人對話",
    "high_dynamic_action": "高速動作",
}

# 派工與候選的狀態
STATUS_NAMES: dict[str, str] = {
    # 生成嘗試的結果（寫在嘗試紀錄裡）
    "success": "成功",
    # 派工與候選的狀態
    "pending_manual": "等待你到平台生成",
    "submitted": "已送出",
    "running": "生成中",
    "completed": "已完成",
    "failed": "失敗",
    "cancelled": "已取消",
    "imported": "已匯入",
    "scored": "已評分",
    "selected": "已選為成片",
    "rejected": "已淘汰",
    "draft": "草稿",
    "cues_ready": "分鏡已就緒",
    "assets_ready": "素材已就緒",
    "preview_ready": "預覽已就緒",
    "approved": "已核准",
    "changes_requested": "需要修改",
}

# 評分維度
DIMENSION_NAMES: dict[str, str] = {
    "identity_consistency": "身份一致性",
    "temporal_stability": "時間穩定性",
    "prompt_adherence": "提示詞貼合",
    "motion_quality": "動態品質",
    "camera_control": "運鏡控制",
    "artifact_severity": "瑕疵嚴重度",
    "facial_acting": "表情演技",
    "cross_shot_identity": "跨鏡頭身份一致",
    "wardrobe_continuity": "服裝連戲",
    "location_continuity": "場景連戲",
    "lip_sync_quality": "嘴型同步",
    "artifact_cleanliness": "畫面乾淨度",
    "usable_shot_rate": "可用鏡頭比例",
    "retries_per_usable": "每支可用所需重試",
    "human_minutes_per_usable": "每支可用的人工分鐘",
}

# 情境獎項
AWARD_NAMES: dict[str, str] = {
    "best_for_character_dialogue": "對話戲最佳",
    "best_for_cinematic_camera": "運鏡最佳",
    "best_for_anime_comic_motion": "動態表現最佳",
    "best_for_high_dynamic_action": "高速動作最佳",
    "best_for_low_retry_production": "最省重試",
}


# 能力代號
CAPABILITY_NAMES: dict[str, str] = {
    "video_i2v": "以首幀生成影片",
    "video_t2v": "以文字生成影片",
    "image_generation": "生成圖片",
    "text_reasoning": "文字推理",
    "speech_to_text": "語音轉文字",
    "text_to_speech": "文字轉語音",
}


# 操作紀錄裡的動作代號
ACTION_NAMES: dict[str, str] = {
    "variants_imported": "匯入影片",
    "shots_dispatched": "建立生成工單",
    "variant_selected": "選為成片",
    "variant_qc_recorded": "記錄評分",
    "transcript_imported": "匯入字幕稿",
    "asr_completed": "語音轉文字完成",
    "review_approved": "審核通過",
    "review_changes_requested": "要求修改",
    "project_created": "建立專案",
    "packaging_optimized": "優化標題文案",
    "metrics_updated": "更新成效數據",
    "assets_registered": "登錄素材",
    "rights_updated": "更新素材授權",
}


def action_name(action: str) -> str:
    """操作紀錄的中文說法。未登錄的代號把底線換成空白。"""
    key = (action or "").strip()
    return ACTION_NAMES.get(key, key.replace("_", " "))


def capability_name(capability: str) -> str:
    return CAPABILITY_NAMES.get((capability or "").strip().lower(), capability)


def shot_name(shot_id: str) -> str:
    """鏡頭的白話名稱。未登錄的鏡頭原樣顯示代號。"""
    return SHOT_NAMES.get(shot_id, shot_id)


def scenario_name(scenario_id: str) -> str:
    return SCENARIO_NAMES.get(scenario_id, scenario_id)


def status_name(status: str) -> str:
    return STATUS_NAMES.get((status or "").strip().lower(), status or "—")


def dimension_name(name: str) -> str:
    return DIMENSION_NAMES.get(name, name)


def award_name(award: str) -> str:
    return AWARD_NAMES.get(award, award.replace("best_for_", "").replace("_", " "))


def provider_name(provider_id: str) -> str:
    """平台的顯示名稱，取自 catalog。查不到時原樣顯示。"""
    try:
        from pipeline.capability.provider_spec import get_provider

        spec = get_provider(provider_id)
        return spec.label if spec else provider_id
    except Exception:  # noqa: BLE001 - 用語表不該讓頁面開不起來
        return provider_id
