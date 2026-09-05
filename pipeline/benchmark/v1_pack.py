# 檔案路徑: video-pipeline/pipeline/benchmark/v1_pack.py
# 產生時間: 2026-09-05 +08:00
# 版本: v1.0
# 模組定位:
#   V1 Benchmark Pack 固定測試案例。
# 主要責任:
#   1. 定義三類情境的角色、場景與鏡頭計畫。
#   2. 列出人工需準備的參考素材清單。
#   3. 提供情境對應表，供結果依 scenario 分組比較。
# 設計約束:
#   所有平台共用同一份 CharacterIdentityPack、Scene、ShotPlan 與 prompt。
#   平台之間只允許 ProviderSpec.parameter_mapping 的格式轉換，
#   不得為個別平台改寫內容，否則比較結果失去意義。
#
#   每顆鏡頭只使用單張首幀作為參考素材。Runway 與 Veo 的參考素材上限
#   為 1（尚未實測，標示為 provisional），若再附加角色圖會被相容性檢查
#   排除，四個平台就無法在同一條件下比較。角色身份改由
#   identity_description 寫入提示詞，這也是 I2V 實際的操作方式。
#
#   片長統一 6 秒，落在四個平台目前登錄範圍的交集內
#   （Kling 5-10s、Runway 5-10s、Veo 4-8s、Seedance 5-10s，皆為 provisional）。
# --------------------------------------------------------------------------

from __future__ import annotations

from pydantic import BaseModel, Field

from pipeline.models.capability import Capability
from pipeline.models.narrative import Beat, BeatIntent, NarrativeIR, Scene
from pipeline.models.reference_asset import ReferenceAssetType
from pipeline.models.shot import (
    CameraMovement,
    CameraSpec,
    CharacterIdentityPack,
    ShotFraming,
    ShotPlan,
)
from pipeline.models.visual_contract import ShotType

BENCHMARK_PACK_VERSION = "v1.0"
BENCHMARK_PROJECT_ID = "benchmark_v1"
SHOT_DURATION_MS = 6000
ASPECT_RATIO = "9:16"

CANDIDATES_PER_SHOT = 3

# 共用的負面提示詞。四個平台使用同一份，不做個別調整。
NEGATIVE_PROMPT = (
    "deformed hands, extra fingers, extra limbs, face morphing, "
    "identity drift, flickering, warping background, text overlay, watermark"
)


class ScenarioKind(BaseModel):
    scenario_id: str
    label: str
    purpose: str
    primary_metrics: list[str] = Field(default_factory=list)


SCENARIOS: tuple[ScenarioKind, ...] = (
    ScenarioKind(
        scenario_id="single_character_cinematic",
        label="單角色 cinematic",
        purpose="測人物身份穩定、走動自然度、表情與慢速運鏡的跟隨能力。",
        primary_metrics=[
            "identity_consistency",
            "cross_shot_identity",
            "camera_control",
            "temporal_stability",
        ],
    ),
    ScenarioKind(
        scenario_id="two_character_dialogue",
        label="雙角色對話",
        purpose=(
            "測兩名角色是否被混淆、視線是否對上、表情演技，"
            "以及 shot-reverse-shot 之間的身份與場景連戲。"
        ),
        primary_metrics=[
            "identity_consistency",
            "cross_shot_identity",
            "facial_acting",
            "location_continuity",
        ],
    ),
    ScenarioKind(
        scenario_id="high_dynamic_action",
        label="高動態",
        purpose="人物、環境與運鏡同時運動，測時間穩定性與結構崩壞程度。",
        primary_metrics=[
            "temporal_stability",
            "motion_quality",
            "artifact_severity",
            "camera_control",
        ],
    ),
)

SCENARIO_BY_ID = {item.scenario_id: item for item in SCENARIOS}


class RequiredAsset(BaseModel):
    asset_id: str
    asset_type: str
    filename: str
    description: str
    # 首幀會實際上傳至平台，比例必須正確；設定圖僅供人工對照，不限比例。
    require_vertical: bool = True


# 人工需準備的素材。每顆鏡頭一張首幀，另兩張角色設定圖僅供人工製作首幀時
# 參考，不會傳送至平台，因此不列入鏡頭的參考素材。
REQUIRED_ASSETS: tuple[RequiredAsset, ...] = (
    RequiredAsset(
        asset_id="ref_char_a_sheet",
        asset_type=ReferenceAssetType.FACE.value,
        filename="char_a_sheet.png",
        description=(
            "角色 A 的視覺設定圖。短髮、深色長風衣的女性，二十多歲。"
            "僅供人工製作首幀時對照，不會上傳至平台。"
        ),
        require_vertical=False,
    ),
    RequiredAsset(
        asset_id="ref_char_b_sheet",
        asset_type=ReferenceAssetType.FACE.value,
        filename="char_b_sheet.png",
        description=(
            "角色 B 的視覺設定圖。中長髮、淺色針織外套的男性，三十多歲。"
            "僅供人工製作首幀時對照，不會上傳至平台。"
        ),
        require_vertical=False,
    ),
    RequiredAsset(
        asset_id="ref_frame_a1",
        asset_type=ReferenceAssetType.FIRST_FRAME.value,
        filename="frame_a1.png",
        description="A1 首幀：角色 A 站在雨夜天橋遠端，全身入鏡，9:16 直式。",
    ),
    RequiredAsset(
        asset_id="ref_frame_a2",
        asset_type=ReferenceAssetType.FIRST_FRAME.value,
        filename="frame_a2.png",
        description="A2 首幀：角色 A 的臉部特寫，雨滴與霓虹反光，9:16 直式。",
    ),
    RequiredAsset(
        asset_id="ref_frame_b1",
        asset_type=ReferenceAssetType.FIRST_FRAME.value,
        filename="frame_b1.png",
        description="B1 首幀：角色 A 與 B 在便利商店內對站，雙人中景，9:16 直式。",
    ),
    RequiredAsset(
        asset_id="ref_frame_b2",
        asset_type=ReferenceAssetType.FIRST_FRAME.value,
        filename="frame_b2.png",
        description=(
            "B2 首幀：越過角色 B 的肩膀看向角色 A，"
            "A 的臉清晰可見，9:16 直式。"
        ),
    ),
    RequiredAsset(
        asset_id="ref_frame_b3",
        asset_type=ReferenceAssetType.FIRST_FRAME.value,
        filename="frame_b3.png",
        description=(
            "B3 首幀：越過角色 A 的肩膀看向角色 B，"
            "B 的臉清晰可見，與 B2 互為反打，9:16 直式。"
        ),
    ),
    RequiredAsset(
        asset_id="ref_frame_c1",
        asset_type=ReferenceAssetType.FIRST_FRAME.value,
        filename="frame_c1.png",
        description="C1 首幀：角色 A 在雨中巷弄起跑，環境有動態元素，9:16 直式。",
    ),
)


def characters() -> list[CharacterIdentityPack]:
    """角色身份。

    canonical_face_ref 刻意留空：V1 只以首幀傳遞身份，
    額外附加角色圖會超出 Runway 與 Veo 的參考素材上限。
    設定圖仍以 REQUIRED_ASSETS 提供，供人工製作首幀時對照。
    """
    return [
        CharacterIdentityPack(
            character_id="char_a",
            role="protagonist",
            identity_description=(
                "A woman in her mid-twenties, short black bob haircut, "
                "dark navy trench coat, pale skin, calm and guarded expression"
            ),
            continuity_notes=[
                "髮型與外套顏色在所有鏡頭中必須一致",
                "不可出現配戴眼鏡或改變髮長的版本",
            ],
        ),
        CharacterIdentityPack(
            character_id="char_b",
            role="counterpart",
            identity_description=(
                "A man in his early thirties, medium-length brown hair, "
                "light grey knitted cardigan, warm but tired expression"
            ),
            continuity_notes=[
                "外套顏色與髮長在所有鏡頭中必須一致",
                "不可與角色 A 的服裝顏色互換",
            ],
        ),
    ]


def narrative() -> NarrativeIR:
    scene_night = Scene(
        scene_id="scene_bridge_night",
        order=0,
        summary="雨夜天橋，角色 A 獨自等待",
        location="pedestrian bridge",
        time_of_day="night",
        mood="tense",
        beats=[
            Beat(
                beat_id="beat_a1", scene_id="scene_bridge_night", order=0,
                intent=BeatIntent.SETUP, summary="A 在雨中緩步走向鏡頭",
                character_ids=["char_a"],
            ),
            Beat(
                beat_id="beat_a2", scene_id="scene_bridge_night", order=1,
                intent=BeatIntent.DEVELOPMENT, summary="A 抬頭，表情轉為警覺",
                character_ids=["char_a"],
            ),
        ],
    )
    scene_store = Scene(
        scene_id="scene_store_interior",
        order=1,
        summary="便利商店內，兩人對話",
        location="convenience store interior",
        time_of_day="night",
        mood="uneasy",
        beats=[
            Beat(
                beat_id="beat_b1", scene_id="scene_store_interior", order=0,
                intent=BeatIntent.SETUP, summary="兩人對站，B 開口",
                character_ids=["char_a", "char_b"],
            ),
            Beat(
                beat_id="beat_b2", scene_id="scene_store_interior", order=1,
                intent=BeatIntent.TURN, summary="A 的反應",
                character_ids=["char_a", "char_b"],
            ),
            Beat(
                beat_id="beat_b3", scene_id="scene_store_interior", order=2,
                intent=BeatIntent.REVEAL, summary="B 的回應",
                character_ids=["char_a", "char_b"],
            ),
        ],
    )
    scene_alley = Scene(
        scene_id="scene_alley_chase",
        order=2,
        summary="雨中巷弄，A 奔跑",
        location="narrow alley",
        time_of_day="night",
        mood="urgent",
        beats=[
            Beat(
                beat_id="beat_c1", scene_id="scene_alley_chase", order=0,
                intent=BeatIntent.TURN, summary="A 起跑，鏡頭跟隨",
                character_ids=["char_a"],
            ),
        ],
    )
    return NarrativeIR(
        project_id=BENCHMARK_PROJECT_ID,
        logline="固定 benchmark 測試素材，非實際劇情",
        scenes=[scene_night, scene_store, scene_alley],
    )


def _shot(
    shot_id: str,
    beat_id: str,
    scene_id: str,
    order: int,
    framing: ShotFraming,
    movement: CameraMovement,
    prompt: str,
    first_frame_ref: str,
    character_refs: list[str],
    camera_notes: str | None = None,
) -> ShotPlan:
    return ShotPlan(
        shot_id=shot_id,
        beat_id=beat_id,
        scene_id=scene_id,
        order=order,
        shot_type=ShotType.GENERATED_IMAGE,
        capability=Capability.VIDEO_I2V,
        camera=CameraSpec(framing=framing, movement=movement, notes=camera_notes),
        prompt=prompt,
        negative_prompt=NEGATIVE_PROMPT,
        character_refs=character_refs,
        first_frame_ref=first_frame_ref,
        target_duration_ms=SHOT_DURATION_MS,
        aspect_ratio=ASPECT_RATIO,
    )


def shots() -> list[ShotPlan]:
    """六顆固定鏡頭，涵蓋三類情境。

    A1/A2 與 B2/B3 刻意成對，用於評估跨鏡頭身份一致性與反打連戲。
    """
    return [
        # --- 情境一：單角色 cinematic ---
        _shot(
            shot_id="bm_a1_walk_slow_push",
            beat_id="beat_a1",
            scene_id="scene_bridge_night",
            order=0,
            framing=ShotFraming.WIDE,
            movement=CameraMovement.DOLLY,
            prompt=(
                "A woman in her mid-twenties with a short black bob and a dark navy "
                "trench coat walks slowly toward the camera on a rain-soaked "
                "pedestrian bridge at night. Neon signs reflect on the wet ground. "
                "The camera pushes in very slowly at a steady pace. "
                "Her face stays clearly visible and unchanged throughout."
            ),
            first_frame_ref="ref_frame_a1",
            character_refs=["char_a"],
            camera_notes="slow dolly in, constant speed, no shake",
        ),
        _shot(
            shot_id="bm_a2_closeup_expression",
            beat_id="beat_a2",
            scene_id="scene_bridge_night",
            order=1,
            framing=ShotFraming.CLOSE_UP,
            movement=CameraMovement.STATIC,
            prompt=(
                "Close-up of the same woman with a short black bob and dark navy "
                "trench coat. She lifts her head and her expression shifts from "
                "calm to alert. Rain drops on her face, neon reflections behind. "
                "The camera holds still. Her facial features remain consistent."
            ),
            first_frame_ref="ref_frame_a2",
            character_refs=["char_a"],
            camera_notes="locked off, no movement",
        ),
        # --- 情境二：雙角色對話與反打 ---
        _shot(
            shot_id="bm_b1_two_shot_dialogue",
            beat_id="beat_b1",
            scene_id="scene_store_interior",
            order=2,
            framing=ShotFraming.MEDIUM,
            movement=CameraMovement.STATIC,
            prompt=(
                "Two people face each other inside a convenience store at night. "
                "On the left, a woman with a short black bob in a dark navy trench "
                "coat. On the right, a man with medium-length brown hair in a light "
                "grey knitted cardigan. The man speaks; the woman listens. "
                "Both remain distinct and do not change appearance. "
                "The camera holds still."
            ),
            first_frame_ref="ref_frame_b1",
            character_refs=["char_a", "char_b"],
            camera_notes="locked off two shot",
        ),
        _shot(
            shot_id="bm_b2_ots_on_a",
            beat_id="beat_b2",
            scene_id="scene_store_interior",
            order=3,
            framing=ShotFraming.OVER_THE_SHOULDER,
            movement=CameraMovement.STATIC,
            prompt=(
                "Over-the-shoulder shot looking past the man in the light grey "
                "cardigan toward the woman with the short black bob in the dark navy "
                "trench coat. She listens, then her eyes narrow slightly. "
                "Her gaze is directed at the man. Convenience store interior at "
                "night. The camera holds still."
            ),
            first_frame_ref="ref_frame_b2",
            character_refs=["char_a", "char_b"],
            camera_notes="OTS favouring character A",
        ),
        _shot(
            shot_id="bm_b3_ots_on_b",
            beat_id="beat_b3",
            scene_id="scene_store_interior",
            order=4,
            framing=ShotFraming.OVER_THE_SHOULDER,
            movement=CameraMovement.STATIC,
            prompt=(
                "Reverse over-the-shoulder shot looking past the woman in the dark "
                "navy trench coat toward the man with medium-length brown hair in "
                "the light grey knitted cardigan. He replies and looks away briefly. "
                "His gaze is directed at the woman. Same convenience store interior "
                "at night. The camera holds still."
            ),
            first_frame_ref="ref_frame_b3",
            character_refs=["char_a", "char_b"],
            camera_notes="reverse of bm_b2_ots_on_a",
        ),
        # --- 情境三：高動態 ---
        _shot(
            shot_id="bm_c1_run_tracking",
            beat_id="beat_c1",
            scene_id="scene_alley_chase",
            order=5,
            framing=ShotFraming.MEDIUM,
            movement=CameraMovement.TRACKING,
            prompt=(
                "The woman with the short black bob and dark navy trench coat runs "
                "through a narrow rain-soaked alley at night. Steam rises from "
                "vents, water splashes under her feet, hanging signs sway. "
                "The camera tracks alongside her at running speed. "
                "Her face and coat stay recognisable despite the motion."
            ),
            first_frame_ref="ref_frame_c1",
            character_refs=["char_a"],
            camera_notes="lateral tracking, matches subject speed",
        ),
    ]


# 鏡頭與情境的對應。結果依此分組，避免產生單一總排名。
SHOT_SCENARIOS: dict[str, str] = {
    "bm_a1_walk_slow_push": "single_character_cinematic",
    "bm_a2_closeup_expression": "single_character_cinematic",
    "bm_b1_two_shot_dialogue": "two_character_dialogue",
    "bm_b2_ots_on_a": "two_character_dialogue",
    "bm_b3_ots_on_b": "two_character_dialogue",
    "bm_c1_run_tracking": "high_dynamic_action",
}

# 需要成對評估跨鏡頭連戲的鏡頭組合（shot_id, ref_shot_id）
CONTINUITY_PAIRS: tuple[tuple[str, str], ...] = (
    ("bm_a2_closeup_expression", "bm_a1_walk_slow_push"),
    ("bm_b2_ots_on_a", "bm_b1_two_shot_dialogue"),
    ("bm_b3_ots_on_b", "bm_b2_ots_on_a"),
    ("bm_c1_run_tracking", "bm_a1_walk_slow_push"),
)

# 可評估表情演技的鏡頭。以鏡頭而非情境判定：
# a2 雖屬單角色情境，但它是特寫且明確要求表情變化，是表情演技的
# 主要觀察對象；a1 的遠景與 c1 的高速奔跑則看不清臉部。
FACIAL_ACTING_SHOTS: frozenset[str] = frozenset(
    {
        "bm_a2_closeup_expression",
        "bm_b1_two_shot_dialogue",
        "bm_b2_ots_on_a",
        "bm_b3_ots_on_b",
    }
)

# V1 沒有音訊，也沒有嘴型的 ground truth，因此嘴型一律不評、不排名。
# 待 short_drama profile 引入對白軌後再啟用。
LIP_SYNC_ENABLED = False


def evaluates_facial_acting(shot_id: str) -> bool:
    return shot_id in FACIAL_ACTING_SHOTS


def target_providers() -> tuple[str, ...]:
    """比較對象涵蓋的平台。

    以 BenchmarkTarget registry 為準而非另外維護一份清單，
    避免兩處不同步時出現「有 target 卻沒派工」的漏測。
    """
    from pipeline.benchmark.target import targets

    return targets().providers


def shots_by_scenario(scenario_id: str) -> list[ShotPlan]:
    return [
        shot for shot in shots() if SHOT_SCENARIOS.get(shot.shot_id) == scenario_id
    ]


def total_generations(target_count: int | None = None) -> int:
    """完整跑完一輪所需的人工生成次數。"""
    from pipeline.benchmark.target import targets

    count = target_count if target_count is not None else len(targets().targets)
    return len(shots()) * count * CANDIDATES_PER_SHOT
