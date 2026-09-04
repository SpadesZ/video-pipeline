# 檔案路徑: video-pipeline/scripts/smoke_capability_router.py
# 產生時間: 2026-09-04 +08:00
# 版本: v1.0
# 模組定位:
#   Capability Router 確定性冒煙測試。
# 主要責任:
#   1. 驗證 catalog（provider / model / routing）互相一致。
#   2. 驗證 model 與 provider 解耦，且相容性檢查會攔截違規請求。
#   3. 驗證路由依 ProductionProfile 的政策欄位而非 preset 名稱。
#   4. 驗證派送的 fallback 軌跡，以及 pending_manual 會終止 fallback。
#   5. 驗證 dispatch_llm_task 的行為契約在重構後不變。
# 說明:
#   第 5 項刻意驗證語義契約而非逐字元快照：檢查 binding 解析、fallback
#   順序、回傳鍵值與無金鑰行為，不因文案或 JSON 鍵序變動而失敗。
#   全程不需要 API 金鑰。
# --------------------------------------------------------------------------

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP_DIR = Path(tempfile.mkdtemp(prefix="router_smoke_"))
os.environ["DATA_DIR"] = str(_TMP_DIR)
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP_DIR / 'smoke.db').as_posix()}"
os.environ["OPENROUTER_API_KEY"] = ""
os.environ["GOOGLE_API_KEY"] = ""
os.environ["SECRETS_FILE"] = str(_TMP_DIR / "missing.env")
os.environ.setdefault("CELERY_BROKER_URL", "memory://")
os.environ.setdefault("CELERY_RESULT_BACKEND", "cache+memory://")

from pipeline.capability import (
    CapabilityRequest,
    CapabilityResult,
    ChatPayload,
    RoutingPolicy,
    VisualPayload,
    check_compatibility,
    clear_adapters,
    dispatch_capability,
    get_provider,
    install_default_adapters,
    load_routing_rules,
    model_registry,
    provider_specs,
    register_adapter,
    registered_adapters,
    routing_policy,
)
from pipeline.adapters.llm.lava_dispatcher import dispatch_llm_task
from pipeline.adapters.llm.task_registry import VIDEO_LLM_TASKS
from pipeline.models.capability import Capability
from pipeline.models.production_profile import load_preset
from pipeline.models.variant import JobStatus


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def visual_request(
    capability: Capability = Capability.VIDEO_I2V,
    duration_ms: int = 6000,
    aspect_ratio: str = "9:16",
    refs: int = 0,
) -> CapabilityRequest:
    return CapabilityRequest(
        request_id="req_smoke",
        capability=capability,
        visual=VisualPayload(
            prompt="雨夜天橋遠景",
            duration_ms=duration_ms,
            aspect_ratio=aspect_ratio,
            reference_asset_ids=[f"ref_{i}" for i in range(refs)],
        ),
    )


def verify_catalog() -> None:
    specs = provider_specs()
    registry = model_registry()
    check(bool(specs), "未載入任何 provider spec")
    check(len(registry) > 0, "未載入任何 model entry")

    # 每個模型宣告的託管平台都必須存在
    for entry in registry.all():
        check(bool(entry.hosted_by), f"{entry.model_id} 未宣告 hosted_by")
        for provider_id in entry.hosted_by:
            check(provider_id in specs, f"{entry.model_id} 指向未登錄平台 {provider_id}")
            spec = specs[provider_id]
            shared = set(entry.capabilities) & set(spec.supported_capabilities)
            check(
                bool(shared),
                f"{entry.model_id} 與平台 {provider_id} 沒有共同能力",
            )

    # 路由規則引用的模型都必須存在且支援該能力
    for rule in load_routing_rules():
        for model_id in rule.prefer_models:
            entry = registry.get(model_id)
            check(entry is not None, f"規則 {rule.rule_id} 指向不存在的模型 {model_id}")
            check(
                entry.supports(rule.capability),
                f"規則 {rule.rule_id} 的 {model_id} 不支援 {rule.capability.value}",
            )


def verify_model_provider_decoupling() -> None:
    registry = model_registry()
    video_models = registry.models_for(Capability.VIDEO_I2V)
    check(len(video_models) >= 2, "應有多個影片模型可供比較")

    # 由平台反查模型，以及由模型反查平台，兩個方向都要成立
    for entry in video_models:
        for provider_id in entry.hosted_by:
            check(
                entry.model_id in {m.model_id for m in registry.models_on(provider_id)},
                f"{provider_id} 的模型清單缺少 {entry.model_id}",
            )
            check(
                provider_id in registry.providers_for(entry.model_id),
                f"{entry.model_id} 的平台清單缺少 {provider_id}",
            )


def verify_compatibility_filtering() -> None:
    registry = model_registry()
    kling = registry.get("kling-video")
    runway = registry.get("runway-gen3")
    check(kling is not None and runway is not None, "測試所需模型不存在")

    kling_spec = get_provider("kling")
    runway_spec = get_provider("runway")

    ok_request = visual_request(duration_ms=6000, aspect_ratio="9:16")
    check(
        not check_compatibility(ok_request, kling, kling_spec),
        "合規請求不應被判定為不相容",
    )

    # 片長超過平台上限
    too_long = visual_request(duration_ms=60_000)
    problems = check_compatibility(too_long, kling, kling_spec)
    check(any("duration" in p for p in problems), f"未攔截超長片長: {problems}")

    # 參考素材數量超過平台上限
    too_many_refs = visual_request(refs=5)
    problems = check_compatibility(too_many_refs, runway, runway_spec)
    check(any("參考素材" in p for p in problems), f"未攔截過多參考素材: {problems}")

    # 模型未由該平台託管
    problems = check_compatibility(ok_request, runway, kling_spec)
    check(any("託管" in p for p in problems), f"未攔截跨平台錯配: {problems}")


def verify_policy_driven_routing() -> None:
    policy = routing_policy()

    vertical = policy.resolve(
        visual_request(aspect_ratio="9:16"), profile=load_preset("comic_drama_high")
    )
    wide = policy.resolve(
        visual_request(aspect_ratio="16:9"), profile=load_preset("film_high")
    )
    check(vertical.ok and wide.ok, "兩種製作方式都應有可用候選")
    check(
        vertical.resolved.model.model_id != wide.resolved.model.model_id,
        "不同製作政策應選出不同模型，否則路由政策未生效",
    )

    # 政策相同但 preset 名稱不同時，結果必須一致
    comic = load_preset("comic_drama_high")
    clone = comic.model_copy(update={"preset_id": "totally_different_name"})
    a = policy.resolve(visual_request(), profile=comic)
    b = policy.resolve(visual_request(), profile=clone)
    check(
        [c.model.model_id for c in a.candidates]
        == [c.model.model_id for c in b.candidates],
        "路由結果不得受 preset_id 名稱影響",
    )

    # 指定偏好平台時應排到最前
    preferred = policy.resolve(
        visual_request(), profile=comic, preferred_provider="seedance"
    )
    check(
        preferred.resolved.provider.provider_id == "seedance",
        "偏好平台應優先於規則順序",
    )

    # 無任何可用模型的能力應明確回報
    empty_policy = RoutingPolicy(rules=[])
    music = CapabilityRequest(request_id="r", capability=Capability.MUSIC)
    check(not empty_policy.resolve(music).ok, "沒有模型的能力應判定無路由")


def verify_dispatch_behaviour() -> None:
    """驗證派送的候選嘗試與終止條件。"""

    class RecordingAdapter:
        def __init__(self, provider: str, status: JobStatus) -> None:
            self.capability = Capability.VIDEO_I2V
            self.provider = provider
            self.status = status
            self.calls = 0

        async def execute(self, request: CapabilityRequest) -> CapabilityResult:
            self.calls += 1
            check(
                "model_id" in request.parameters,
                "router 應將選定的模型傳入轉接器",
            )
            if self.status is JobStatus.COMPLETED:
                return CapabilityResult(
                    ok=True, status=self.status, provider=self.provider
                )
            return CapabilityResult(
                ok=False,
                status=self.status,
                provider=self.provider,
                error_message=f"{self.provider} 失敗",
            )

    # 清空登錄表以驗證「找不到轉接器」的行為。內建轉接器於本函式結尾復原。
    clear_adapters()

    request = visual_request()
    profile = load_preset("comic_drama_high")
    result = asyncio.run(dispatch_capability(request, profile=profile))
    check(not result.ok, "未登錄轉接器時不應成功")
    check(
        all(a.status == "no_adapter" for a in result.attempts),
        f"應全部回報 no_adapter: {[a.status for a in result.attempts]}",
    )
    check(len(result.attempts) >= 2, "應嘗試過多個候選")

    # 首選失敗時應退到次選
    order = [c.provider.provider_id for c in routing_policy().resolve(
        request, profile=profile
    ).candidates]
    failing = RecordingAdapter(order[0], JobStatus.FAILED)
    succeeding = RecordingAdapter(order[1], JobStatus.COMPLETED)
    register_adapter(failing)
    register_adapter(succeeding)

    result = asyncio.run(dispatch_capability(request, profile=profile))
    check(result.ok, "次選可用時整體應成功")
    check(result.provider == order[1], f"應由次選完成，實際 {result.provider}")
    check(failing.calls == 1 and succeeding.calls == 1, "兩個候選各應被嘗試一次")
    check(len(result.attempts) == 2, f"fallback 軌跡應有兩筆: {result.attempts}")

    # pending_manual 必須終止 fallback，否則同一鏡頭會被重複派工
    manual = RecordingAdapter(order[0], JobStatus.PENDING_MANUAL)
    later = RecordingAdapter(order[1], JobStatus.COMPLETED)
    register_adapter(manual)
    register_adapter(later)

    result = asyncio.run(dispatch_capability(request, profile=profile))
    check(result.awaiting_human, "pending_manual 應被視為等待人工")
    check(not result.is_terminal, "pending_manual 不是終態")
    check(later.calls == 0, "等待人工時不應繼續嘗試其他平台")

    # 復原內建轉接器，避免影響後續驗證
    clear_adapters()
    install_default_adapters()
    installed = registered_adapters()
    check(
        any(cap is Capability.TEXT_REASONING for cap, _p in installed),
        "復原後應含文字推理轉接器",
    )
    check(
        any(cap is Capability.VIDEO_I2V for cap, _p in installed),
        "復原後應含人工影片轉接器",
    )


def verify_legacy_contract() -> None:
    """dispatch_llm_task 的行為契約在重構後必須維持。"""
    task_ids = [task.task_id for task in VIDEO_LLM_TASKS]
    check(len(task_ids) == 6, f"既有任務數量應為 6，實際 {len(task_ids)}")

    messages = [{"role": "user", "content": "ping"}]

    # 未知任務
    unknown = asyncio.run(dispatch_llm_task("does_not_exist", messages))
    check(unknown["ok"] is False, "未知任務應失敗")
    check("Unknown video LLM task" in unknown["error"], "未知任務錯誤訊息格式改變")

    # 無金鑰時每個任務都應以相同形態失敗
    for task_id in task_ids:
        result = asyncio.run(dispatch_llm_task(task_id, messages))
        check(result["ok"] is False, f"{task_id} 無金鑰時應失敗")
        check("error" in result, f"{task_id} 缺少 error 鍵")
        check(
            f"All LLM connections failed for task '{task_id}'" in result["error"],
            f"{task_id} 錯誤訊息契約改變: {result['error']}",
        )
        check("fallback_order" in result, f"{task_id} 缺少 fallback_order 鍵")
        check(
            isinstance(result["fallback_order"], list) and result["fallback_order"],
            f"{task_id} 的 fallback_order 應為非空清單",
        )
        for entry in result["fallback_order"]:
            check("connection_id" in entry, "fallback 項目缺少 connection_id")
            check("status" in entry, "fallback 項目缺少 status")

    # 相同任務重複解析必須得到相同的 connection 順序
    first = asyncio.run(dispatch_llm_task(task_ids[0], messages))
    second = asyncio.run(dispatch_llm_task(task_ids[0], messages))
    check(
        [e["connection_id"] for e in first["fallback_order"]]
        == [e["connection_id"] for e in second["fallback_order"]],
        "同一任務的 fallback 順序必須穩定",
    )

    # 綁定不同平台的任務，其首選 connection 應不同
    connections = {
        task_id: asyncio.run(dispatch_llm_task(task_id, messages))["fallback_order"][0][
            "connection_id"
        ]
        for task_id in task_ids
    }
    check(
        len(set(connections.values())) >= 2,
        f"不同任務應解析到不同的首選 connection: {connections}",
    )


def verify_request_hash() -> None:
    """內容相同的請求必須得到相同雜湊，供 job manifest 冪等使用。"""
    a = CapabilityRequest(
        request_id="id_a",
        capability=Capability.VIDEO_I2V,
        visual=VisualPayload(prompt="same", duration_ms=5000),
    )
    b = CapabilityRequest(
        request_id="id_b",
        capability=Capability.VIDEO_I2V,
        visual=VisualPayload(prompt="same", duration_ms=5000),
    )
    c = CapabilityRequest(
        request_id="id_a",
        capability=Capability.VIDEO_I2V,
        visual=VisualPayload(prompt="different", duration_ms=5000),
    )
    check(a.content_hash() == b.content_hash(), "request_id 不應影響內容雜湊")
    check(a.content_hash() != c.content_hash(), "內容不同時雜湊必須不同")

    chat = CapabilityRequest(
        request_id="x",
        capability=Capability.TEXT_REASONING,
        chat=ChatPayload(messages=[{"role": "user", "content": "hi"}]),
    )
    check(chat.active_payload is not None, "chat 請求應可取得 payload")


def main() -> int:
    verify_catalog()
    verify_model_provider_decoupling()
    verify_compatibility_filtering()
    verify_policy_driven_routing()
    verify_request_hash()
    verify_dispatch_behaviour()
    verify_legacy_contract()

    print(
        f"OK capability router smoke providers={len(provider_specs())} "
        f"models={len(model_registry())} rules={len(load_routing_rules())} "
        f"adapters={len(registered_adapters())}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
