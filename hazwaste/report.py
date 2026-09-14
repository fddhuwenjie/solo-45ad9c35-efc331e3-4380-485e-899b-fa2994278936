"""逐桶处置单：把一次冻结修订中的计算明细按单桶汇总。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import matrix as M


def build_disposal_sheet(rev: Dict[str, Any],
                         container_id: Optional[str]) -> Dict[str, Any]:
    result = rev["result"]
    state = rev["state"]
    if container_id is None:
        # 未指定桶时返回汇总清单（逐桶索引）
        return {
            "revision_id": rev["revision_id"],
            "matrix_version": rev["matrix_version"],
            "matrix_fingerprint": rev["matrix_fingerprint"],
            "disposition": rev["disposition"],
            "note": "未指定 container_id，返回全部桶的处置索引；"
                    "加 ?container_id=<id> 获取逐桶明细",
            "containers": [_index_entry(c) for c in result["per_container"]],
        }

    if container_id not in state:
        from .storage import StoreError
        raise StoreError(
            "CONTAINER_NOT_FOUND",
            f"修订 {rev['revision_id']} 中不存在容器 {container_id}", 404)

    c = state[container_id]
    pc = next(x for x in result["per_container"]
              if x["container_id"] == container_id)
    calc = result["calculation"]
    cls = calc["classification"][container_id]
    decl = calc["declared_check"][container_id]
    limits = calc["limits"]

    # ---- 装填率明细 ----
    cap_f = float(c["capacity_l"])
    fill_f = float(c["volume_l"])
    fill_detail = {
        "capacity_l": cap_f,
        "volume_l": fill_f,
        "fill_ratio": round(fill_f / cap_f, 6) if cap_f > 0 else None,
        "limit_max_fill_ratio": limits["max_fill_ratio"],
        "headspace_l": round(cap_f - fill_f, 6),
        "formula": "fill_ratio = volume_l / capacity_l",
        "ok": (cap_f > 0
               and fill_f / cap_f <= limits["max_fill_ratio"] + 1e-9),
    }

    # ---- 成对规则明细 ----
    pairs = []
    for p in calc["pairs"]:
        if container_id not in p["containers"]:
            continue
        other = p["containers"][1] if p["containers"][0] == container_id \
            else p["containers"][0]
        pairs.append({
            "other_container": other,
            "other_position": p["positions"][other],
            "matched_rules": p["matched_rules"],
            "possible_rules": p["possible_rules"],
            "colocation_level": p["colocation_level"],
            "violations": p["violations"],
            "distance_checks": p.get("distance_checks"),
            "ventilation_checks": p.get("ventilation_checks"),
        })

    # ---- 托盘盛漏明细 ----
    tray = None
    pos = c.get("position") or {}
    if pos.get("tray_id"):
        for t in calc["trays"]:
            if t["tray_id"] == pos["tray_id"]:
                tray = {k: v for k, v in t.items() if k != "_issues"}
                tray["formula"] = ("required_containment_l = "
                                   "sum(volume_l) * tray_capacity_ratio")
                break

    # ---- 合并自检明细（源桶两两禁配核查） ----
    merge_check = None
    for m in calc.get("merges", []):
        if m["merged_container"] == container_id:
            merge_check = {k: v for k, v in m.items() if k != "_issues"}
            break

    # ---- 本桶相关问题（含阻断与警告），给出依据与整改建议 ----
    my_issues = [i for i in result["issues"]
                 if container_id in i["containers"]]
    actions = []
    seen_actions = set()
    for i in my_issues:
        for act in _remediations(i, container_id):
            if act not in seen_actions:
                seen_actions.add(act)
                actions.append(act)

    return {
        "sheet_type": "per_container_disposal",
        "revision_id": rev["revision_id"],
        "created_ts": rev["created_ts"],
        "matrix_version": rev["matrix_version"],
        "matrix_fingerprint": rev["matrix_fingerprint"],
        "facility_id": rev["facility_id"],
        "facility_version": rev["facility_version"],
        "layout_disposition": rev["disposition"],
        "container": {
            "id": container_id,
            "position": c.get("position"),
            "material": pc["material"],
            "components": c.get("components", []),
            "declared_hazard_classes": c.get("hazard_classes", []),
            "merged_from": c.get("merged_from"),
            "merge_provenance": c.get("merge_provenance"),
            "replaced_from": c.get("replaced_from"),
        },
        "classification_detail": {
            "definite": cls["definite"],
            "possible": cls["possible"],
            "ambiguous": cls["ambiguous"],
            "unknown_components": cls["unknown_components"],
            "band_hits": cls["band_hits"],
            "declared_check": decl,
            "formula": "成分浓度区间 ∩ 矩阵阈值带 -> 确定集/可能集",
        },
        "fill_check": fill_detail,
        "pair_checks": pairs,
        "merge_self_check": merge_check,
        "tray_containment_check": tray,
        "issues": [{
            "code": i["code"],
            "severity": i["severity"],
            "blocking": i["blocking"],
            "message": i["message"],
            "positions": i["positions"],
            "basis": i["basis"],
        } for i in my_issues],
        "container_disposition": pc["disposition"],
        "required_actions": actions,
        "result_fingerprint": result["fingerprint"],
        "fingerprint_basis": result["fingerprint_basis"],
    }


def _index_entry(pc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "container_id": pc["container_id"],
        "position": pc["position"],
        "hazard_definite": pc["hazard_definite"],
        "hazard_possible": pc["hazard_possible"],
        "ambiguous": pc["ambiguous"],
        "fill_ratio": pc["fill_ratio"],
        "material": pc["material"],
        "issues": pc["issues"],
        "disposition": pc["disposition"],
    }


def _remediations(issue: Dict[str, Any], cid: str) -> List[str]:
    code = issue["code"]
    rule = issue.get("basis", {}).get("rule") or {}
    coloc = rule.get("colocation")
    if code in ("INCOMPATIBLE_COLOCATION",
                "POSSIBLE_INCOMPATIBLE_COLOCATION"):
        other = [x for x in issue["containers"] if x != cid]
        other_s = other[0] if other else "对方"
        if coloc == M.COLOC_FORBID:
            return [f"将 {cid} 与 {other_s} 分柜存放（规则 "
                    f"{rule.get('id')}: {rule.get('note', '')}）"]
        if coloc == M.COLOC_ZONE:
            return [f"将 {cid} 与 {other_s} 移至不同分区，间距≥"
                    f"{rule.get('min_distance_m')}m，并满足通风要求 "
                    f"{rule.get('same_ventilation')}"]
        if coloc == M.COLOC_TRAY:
            return [f"将 {cid} 与 {other_s} 分到不同盛漏托盘"]
    if code in ("MERGE_INCOMPATIBLE", "MERGE_POSSIBLY_INCOMPATIBLE"):
        src = issue.get("basis", {}).get("source_pair", [])
        return [f"禁止该合并：源桶 {src} 在桶内混合会触发规则 "
                f"{rule.get('id')}（{rule.get('note', '')}）；"
                "应分桶分柜存放，不得并入同一容器"]
    if code == "TRAY_CONTAINMENT_INSUFFICIENT":
        d = issue["basis"].get("deficit_l")
        return [f"更换更大托盘或分装，盛漏有效容积至少增加 {d}L"
                "（有效容积须≥总装量×规定倍数）"]
    if code == "FILL_OVERFLOW":
        return [f"分装或更换更大容器，使装填率≤"
                f"{issue['basis']['limit']}"]
    if code == "CLASS_AMBIGUOUS":
        return ["缩窄成分浓度范围（提供实测浓度）后重新提交更正事件，"
                "以消除多结论；此前保持待处置"]
    if code == "COMP_UNKNOWN":
        return ["补充未收录成分的危害数据并冻结新矩阵版本后再复核"]
    if code == "DECLARED_MISMATCH":
        return ["核对标签与实际成分，通过 correct 事件更正成分或危害类别"]
    if code in ("MATERIAL_INCOMPATIBLE", "MATERIAL_POSSIBLY_BAD",
                "MATERIAL_UNKNOWN"):
        return ["更换与危害类别适配的容器材质（repack 换桶事件）"]
    if code == "VENTILATION_INSUFFICIENT":
        return [f"移至 {issue['basis']['required']} 或更高级别通风的柜体"]
    if code == "POSITION_INVALID":
        return ["修正位置：柜体/分区/托盘必须存在且层级归属正确"]
    if code == "RULE_CONFLICT":
        return ["规则冲突需由管理员冻结修订后的矩阵版本后重新复核"]
    return ["按问题依据处理后重新提交事件并重算布局"]
