"""复核引擎（纯函数，无 IO）。

输入：冻结矩阵 + 设施快照 + 容器快照（含当前位置）。
输出：布局复核结果——逐桶分类与处置单、成对反应核查、托盘盛漏核算、
隔离距离 / 通风核查、问题清单（涉事容器、位置、依据、计算明细）。

成分以“浓度范围”给出：当范围跨越矩阵中多个危害阈值带时，危害类别同时
存在“确定集”和“可能集”，由此产生的多种结论与规则冲突均使方案保持
待处置（pending）。
"""
from __future__ import annotations

import hashlib
import json
from itertools import product
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from . import matrix as M

EPS = 1e-9


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha_fp(obj: Any, length: int = 16) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()[:length]


def _norm_comp_name(name: str) -> str:
    return (name or "").strip().lower()


# --------------------------------------------------------------------------- #
# 成分浓度范围 -> 危害类别
# --------------------------------------------------------------------------- #
def classify_components(
    matrix: Dict[str, Any], components: Sequence[Dict[str, Any]]
) -> Dict[str, Any]:
    """把成分浓度范围归并为容器的危害类别状态。

    对每个成分，找出与 [conc_min, conc_max] 有交集的全部阈值带：
    - 交满整个区间的带贡献“确定类别”；
    - 只覆盖部分区间的带只贡献“可能类别”。
    多个成分之间：确定集取并集；可能集取并集。命中多个不同类别集合即多结论。
    """
    table = matrix.get("component_class", {})
    definite: Set[str] = set()
    possible: Set[str] = set()
    unknown: List[str] = []
    band_hits: List[Dict[str, Any]] = []
    errors: List[str] = []
    span = 0.0

    for comp in components:
        name = _norm_comp_name(comp.get("name", ""))
        try:
            lo = float(comp.get("conc_min"))
            hi = float(comp.get("conc_max"))
        except (TypeError, ValueError):
            errors.append(f"成分 {name} 浓度不是数值")
            continue
        if lo < -EPS or hi < -EPS or lo > hi + EPS:
            errors.append(f"成分 {name} 浓度范围非法: [{lo},{hi}]")
            continue
        if name not in table:
            unknown.append(comp.get("name", name))
            band_hits.append({"component": comp.get("name", name),
                              "range": [lo, hi], "bands": [], "known": False})
            continue

        span = max(span, hi - lo)
        bands = table[name]
        hits: List[Dict[str, Any]] = []
        comp_pos: Set[str] = set()
        # 与范围有正宽度交集的带：其类别至少“可能成立”。
        # 范围被阈值切成多段时，各段都出现的类别“确定成立”（取交集），
        # 仅部分段出现的类别只“可能成立”。单带完整覆盖时交集即该带类别。
        # 点浓度（conc_min == conc_max，如 HCl [30,30]）按“该点是否落在
        # 带内（含边界）”命中；点恰在阈值边界时相邻两带都命中，按多结论从严。
        degenerate = hi - lo <= EPS
        segment_classes: List[Set[str]] = []
        for blo, bhi, classes in bands:
            if degenerate:
                in_band = (blo - EPS <= lo <= bhi + EPS)
                overlap = 0.0
            else:
                overlap = min(hi, bhi) - max(lo, blo)
                in_band = overlap > EPS  # 仅临界点相接（宽度 0）不算覆盖
            if not in_band:
                continue
            covers_full = (lo >= blo - EPS) and (hi <= bhi + EPS)
            hits.append({"band": [blo, bhi], "classes": list(classes),
                         "overlap": round(max(0.0, overlap), 6),
                         "covers_full_range": covers_full,
                         "point_concentration": degenerate})
            comp_pos.update(classes)
            segment_classes.append(set(classes))
        comp_def: Set[str] = (set.intersection(*segment_classes)
                              if segment_classes else set())
        # 该成分未被任何带覆盖（矩阵表有空洞）——按未知处理
        if not hits:
            unknown.append(comp.get("name", name))
        band_hits.append({"component": comp.get("name", name),
                          "range": [lo, hi], "bands": hits, "known": True})
        if comp_def:
            definite.update(comp_def)
        possible.update(comp_pos)

    # 多结论判定：可能集严格大于确定集，或不同阈值带推出不同类别集合
    ambiguous = bool(possible - definite)
    if not ambiguous:
        class_sets = {tuple(sorted(h["classes"]))
                      for bh in band_hits for h in bh["bands"]}
        if len(class_sets) > 1:
            ambiguous = True
    return {
        "definite": sorted(definite),
        "possible": sorted(possible),
        "ambiguous": ambiguous,
        "unknown_components": sorted(set(unknown)),
        "band_hits": band_hits,
        "errors": errors,
    }


def infer_container(matrix: Dict[str, Any], container: Dict[str, Any]) -> Dict[str, Any]:
    """单桶推断：危害类别状态 + 声明类别核对。"""
    cls = classify_components(matrix, container.get("components", []))
    declared = sorted(set(container.get("hazard_classes") or []))
    declared_unknown = sorted(set(declared) - set(M.HAZARD_CLASSES))

    basis: List[Dict[str, Any]] = []
    # 声明类别与推断结果核对
    decl_conflicts: List[Dict[str, Any]] = []
    for d in declared:
        if d not in cls["possible"]:
            decl_conflicts.append({
                "declared": d,
                "inferred_definite": cls["definite"],
                "inferred_possible": cls["possible"],
            })
    if cls["definite"]:
        basis.append({"kind": "inferred_definite", "classes": cls["definite"]})
    if cls["ambiguous"]:
        basis.append({"kind": "inferred_possible", "classes": cls["possible"]})
    if declared:
        basis.append({"kind": "declared", "classes": declared})
    return {
        "container_id": container["id"],
        "classification": cls,
        "declared": declared,
        "declared_unknown": declared_unknown,
        "declared_conflicts": decl_conflicts,
        "basis": basis,
    }


# --------------------------------------------------------------------------- #
# 设施空间索引
# --------------------------------------------------------------------------- #
def build_spatial_index(facility: Dict[str, Any]) -> Dict[str, Any]:
    """cabinet_id -> 分区、托盘、通风；tray_id/zone_id -> 父级。"""
    cabinets: Dict[str, Dict[str, Any]] = {}
    tray_parent: Dict[str, str] = {}
    for cab in facility.get("cabinets", []):
        cid = cab["id"]
        cabinets[cid] = {
            "id": cid,
            "ventilation": cab.get("ventilation", "none"),
            "zones": {z["id"]: z for z in cab.get("zones", [])},
            "trays": {},
        }
        for z in cab.get("zones", []):
            for t in z.get("trays", []):
                tray_parent[t["id"]] = z["id"]
                cabinets[cid]["trays"][t["id"]] = t
    return {"cabinets": cabinets, "tray_zone": tray_parent}


def _locate(position: Optional[Dict[str, Any]],
            spatial: Dict[str, Any]) -> Dict[str, Any]:
    """解析位置 -> (cabinet, zone, tray)，并返回位置层级与错误。"""
    out: Dict[str, Any] = {
        "cabinet_id": None, "zone_id": None, "tray_id": None,
        "level": "unplaced", "error": None,
    }
    if not position:
        return out
    cab_id = position.get("cabinet_id")
    zone_id = position.get("zone_id")
    tray_id = position.get("tray_id")
    cab = spatial["cabinets"].get(cab_id)
    if cab is None:
        out.update(cabinet_id=cab_id, error=f"柜体 {cab_id} 不存在")
        return out
    out["cabinet_id"] = cab_id
    out["ventilation"] = cab["ventilation"]
    if zone_id is None:
        out["level"] = "cabinet"
        return out
    if zone_id not in cab["zones"]:
        out["error"] = f"分区 {zone_id} 不在柜体 {cab_id} 内"
        return out
    out["zone_id"] = zone_id
    out["level"] = "zone"
    if tray_id is None:
        return out
    if tray_id not in cab["trays"]:
        out["error"] = f"托盘 {tray_id} 不在柜体 {cab_id} 内"
        return out
    if spatial["tray_zone"].get(tray_id) != zone_id:
        out["error"] = (f"托盘 {tray_id} 属于分区 "
                        f"{spatial['tray_zone'].get(tray_id)} 而非 {zone_id}")
        return out
    out["tray_id"] = tray_id
    out["level"] = "tray"
    return out


def coloc_level(a: Dict[str, Any], b: Dict[str, Any]) -> Optional[str]:
    """两桶当前位置的共置层级。不同柜/任一未放置 -> None。"""
    if a["cabinet_id"] is None or b["cabinet_id"] is None:
        return None
    if a["cabinet_id"] != b["cabinet_id"]:
        return "separate_cabinet"
    if a["zone_id"] != b["zone_id"]:
        return "same_cabinet"
    if a["tray_id"] != b["tray_id"]:
        return "same_zone"
    return "same_tray"


def _zone_distance(facility: Dict[str, Any], cab_id: str,
                   z1: str, z2: str) -> Optional[float]:
    for cab in facility.get("cabinets", []):
        if cab["id"] != cab_id:
            continue
        p1 = p2 = None
        for z in cab.get("zones", []):
            if z["id"] == z1:
                p1 = z.get("position_xy")
            if z["id"] == z2:
                p2 = z.get("position_xy")
        if p1 is not None and p2 is not None:
            return ((p1[0] - p2[0]) ** 2 + (p1[1] - p2[1]) ** 2) ** 0.5
    return None


# --------------------------------------------------------------------------- #
# 通风
# --------------------------------------------------------------------------- #
def _vent_ok(matrix: Dict[str, Any], have: str, need: str) -> Tuple[bool, int]:
    levels = matrix["ventilation_levels"]
    gap = levels.get(need, 0) - levels.get(have, 0)
    return gap <= 0, gap


def _ventilation_requirement(matrix: Dict[str, Any],
                             classes: Sequence[str]) -> Optional[Tuple[str, str]]:
    """一组危害类别中取通风要求最高者。"""
    best: Optional[Tuple[str, str]] = None
    best_level = -1
    for c in classes:
        req = matrix.get("class_ventilation", {}).get(c)
        if req is None:
            continue
        lvl = matrix["ventilation_levels"][req[0]]
        if lvl > best_level:
            best_level = lvl
            best = req
    return best


# --------------------------------------------------------------------------- #
# 问题清单构造
# --------------------------------------------------------------------------- #
def _issue(code: str, severity: str, subject: str, message: str,
           containers: Sequence[str], positions: Dict[str, Any],
           basis: Dict[str, Any], blocking: bool = True) -> Dict[str, Any]:
    """统一问题结构。

    code      : 问题代码（machine readable）
    severity  : blocker(禁，方案待处置) / warning(警告，仍须复核确认)
    subject   : pair | tray | container
    blocking  : 是否阻断“可处置”判定
    """
    return {
        "code": code,
        "severity": severity,
        "subject": subject,
        "message": message,
        "containers": list(containers),
        "positions": positions,
        "basis": basis,
        "blocking": blocking,
    }


# --------------------------------------------------------------------------- #
# 主评估
# --------------------------------------------------------------------------- #
def evaluate_layout(matrix: Dict[str, Any], facility: Dict[str, Any],
                    containers: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """对整个布局做一次完整复核（每次移动后重算即重新调用本函数）。"""
    limits = matrix["limits"]
    spatial = build_spatial_index(facility)
    issues: List[Dict[str, Any]] = []

    # ---- 逐桶推断与位置解析 ---------------------------------------------- #
    inferred: Dict[str, Dict[str, Any]] = {}
    located: Dict[str, Dict[str, Any]] = {}
    for c in containers:
        inf = infer_container(matrix, c)
        inferred[c["id"]] = inf
        loc = _locate(c.get("position"), spatial)
        located[c["id"]] = loc

        cls = inf["classification"]
        if cls["errors"]:
            issues.append(_issue(
                "COMP_DATA_ERROR", "blocker", "container",
                f"{c['id']} 成分数据错误: {'; '.join(cls['errors'])}",
                [c["id"]], {c["id"]: c.get("position")},
                {"errors": cls["errors"], "band_hits": cls["band_hits"]}))
        if cls["unknown_components"]:
            issues.append(_issue(
                "COMP_UNKNOWN", "blocker", "container",
                f"{c['id']} 含矩阵未收录成分 {cls['unknown_components']}，"
                "无法判定相容性，方案保持待处置",
                [c["id"]], {c["id"]: c.get("position")},
                {"unknown": cls["unknown_components"],
                 "band_hits": cls["band_hits"]}))
        if cls["ambiguous"]:
            issues.append(_issue(
                "CLASS_AMBIGUOUS", "blocker", "container",
                f"{c['id']} 成分浓度范围跨越危害阈值，类别存在多种结论: "
                f"确定 {cls['definite']} / 可能 {cls['possible']}",
                [c["id"]], {c["id"]: c.get("position")},
                {"definite": cls["definite"], "possible": cls["possible"],
                 "band_hits": cls["band_hits"]}))
        if inf["declared_unknown"]:
            issues.append(_issue(
                "DECLARED_CLASS_UNKNOWN", "blocker", "container",
                f"{c['id']} 声明了未知危害类别 {inf['declared_unknown']}",
                [c["id"]], {c["id"]: c.get("position")},
                {"declared": inf["declared"]}))
        if inf["declared_conflicts"]:
            issues.append(_issue(
                "DECLARED_MISMATCH", "blocker", "container",
                f"{c['id']} 标签声明类别与成分推断冲突",
                [c["id"]], {c["id"]: c.get("position")},
                {"conflicts": inf["declared_conflicts"],
                 "band_hits": cls["band_hits"]}))
        if loc["error"]:
            issues.append(_issue(
                "POSITION_INVALID", "blocker", "container",
                f"{c['id']} 位置无效: {loc['error']}",
                [c["id"]], {c["id"]: c.get("position")},
                {"error": loc["error"]}))
        elif loc["cabinet_id"] is None:
            issues.append(_issue(
                "UNPLACED", "blocker", "container",
                f"{c['id']} 尚未分配柜体位置，须先试排/移位定位后方可处置",
                [c["id"]], {c["id"]: None},
                {"position": c.get("position")}))

        # ---- 材质适配 ---------------------------------------------------- #
        material = M.normalize_material(matrix, c.get("material", ""))
        incompatible = matrix.get("material_compat", {}).get(material)
        if incompatible is None:
            issues.append(_issue(
                "MATERIAL_UNKNOWN", "blocker", "container",
                f"{c['id']} 材质 {c.get('material')} 未在矩阵中登记",
                [c["id"]], {c["id"]: c.get("position")},
                {"material": c.get("material"), "normalized": material}))
        else:
            definite_bad = sorted(set(incompatible) & set(cls["definite"]))
            possible_bad = sorted(set(incompatible) & set(cls["possible"]))
            if definite_bad:
                issues.append(_issue(
                    "MATERIAL_INCOMPATIBLE", "blocker", "container",
                    f"{c['id']} 材质 {material} 与确定危害类别 "
                    f"{definite_bad} 不适配",
                    [c["id"]], {c["id"]: c.get("position")},
                    {"material": material,
                     "incompatible_classes": incompatible,
                     "definite_classes": cls["definite"],
                     "matched": definite_bad}))
            elif possible_bad:
                issues.append(_issue(
                    "MATERIAL_POSSIBLY_BAD", "blocker", "container",
                    f"{c['id']} 材质 {material} 在浓度范围高端可能与 "
                    f"{possible_bad} 不适配",
                    [c["id"]], {c["id"]: c.get("position")},
                    {"material": material,
                     "incompatible_classes": incompatible,
                     "possible_classes": cls["possible"],
                     "matched": possible_bad}))

        # ---- 装填率 ------------------------------------------------------ #
        cap = c.get("capacity_l")
        fill = c.get("volume_l")
        try:
            cap_f = float(cap)
            fill_f = float(fill)
            ratio = fill_f / cap_f if cap_f > 0 else None
        except (TypeError, ValueError, ZeroDivisionError):
            cap_f = fill_f = None
            ratio = None
        if ratio is None or cap_f is None:
            issues.append(_issue(
                "FILL_DATA_ERROR", "blocker", "container",
                f"{c['id']} 容量/装量数据无效: capacity={cap}, volume={fill}",
                [c["id"]], {c["id"]: c.get("position")},
                {"capacity_l": cap, "volume_l": fill}))
        else:
            if fill_f < -EPS or ratio < -EPS:
                issues.append(_issue(
                    "FILL_DATA_ERROR", "blocker", "container",
                    f"{c['id']} 装量为负",
                    [c["id"]], {c["id"]: c.get("position")},
                    {"capacity_l": cap_f, "volume_l": fill_f}))
            elif ratio > limits["max_fill_ratio"] + EPS:
                issues.append(_issue(
                    "FILL_OVERFLOW", "blocker", "container",
                    f"{c['id']} 装填率 {ratio:.3f} 超过上限 "
                    f"{limits['max_fill_ratio']}",
                    [c["id"]], {c["id"]: c.get("position")},
                    {"fill_ratio": round(ratio, 6),
                     "limit": limits["max_fill_ratio"],
                     "capacity_l": cap_f, "volume_l": fill_f,
                     "headspace_l": round(cap_f - fill_f, 6)}))

        # ---- 单桶通风要求 ------------------------------------------------ #
        if loc["cabinet_id"] is not None and not loc["error"]:
            vent_req = _ventilation_requirement(matrix, cls["possible"]
                                                if cls["ambiguous"]
                                                else cls["definite"])
            if vent_req:
                need_level, why = vent_req
                have = spatial["cabinets"][loc["cabinet_id"]]["ventilation"]
                ok, gap = _vent_ok(matrix, have, need_level)
                if not ok:
                    issues.append(_issue(
                        "VENTILATION_INSUFFICIENT", "blocker", "container",
                        f"{c['id']} 所在柜 {loc['cabinet_id']} 通风 {have} "
                        f"不满足要求 {need_level}（{why}）",
                        [c["id"]],
                        {c["id"]: c.get("position")},
                        {"required": need_level, "actual": have,
                         "level_gap": gap, "reason": why,
                         "classes_basis": (cls["possible"]
                                           if cls["ambiguous"]
                                           else cls["definite"])}))

    # ---- 成对反应核查 ---------------------------------------------------- #
    pair_details: List[Dict[str, Any]] = []
    ids = [c["id"] for c in containers]
    by_id = {c["id"]: c for c in containers}
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a_id, b_id = ids[i], ids[j]
            detail = _evaluate_pair(matrix, facility, spatial,
                                    by_id[a_id], by_id[b_id],
                                    inferred[a_id], inferred[b_id],
                                    located[a_id], located[b_id])
            pair_details.append(detail)
            issues.extend(detail.pop("_issues"))

    # ---- 合并桶自检：源桶废液在桶内混合，按合并前溯源两两核查禁配反应 ---- #
    merge_details = []
    for c in containers:
        prov = c.get("merge_provenance")
        if prov:
            md = _evaluate_merge(matrix, c, prov)
            merge_details.append(md)
            issues.extend(md.pop("_issues"))

    # ---- 托盘盛漏核算 ---------------------------------------------------- #
    tray_details = _evaluate_trays(matrix, spatial, containers, located)
    for td in tray_details:
        issues.extend(td.pop("_issues"))

    # ---- 汇总 ------------------------------------------------------------ #
    blocking = [x for x in issues if x["blocking"]]
    warnings = [x for x in issues if not x["blocking"]]
    disposition = "pending" if blocking else "disposable"

    per_container = []
    for c in containers:
        cid = c["id"]
        cls = inferred[cid]["classification"]
        try:
            cap_f = float(c["capacity_l"])
            fill_f = float(c["volume_l"])
            ratio_safe = round(fill_f / cap_f, 6) if cap_f > 0 else None
        except (TypeError, ValueError):
            cap_f = fill_f = 0.0
            ratio_safe = None
        my_issues = [k for k, x in enumerate(issues)
                     if cid in x["containers"]]
        per_container.append({
            "container_id": cid,
            "position": c.get("position"),
            "hazard_definite": cls["definite"],
            "hazard_possible": cls["possible"],
            "ambiguous": cls["ambiguous"],
            "declared": inferred[cid]["declared"],
            "fill_ratio": ratio_safe,
            "material": M.normalize_material(matrix, c.get("material", "")),
            "issues": [issues[k]["code"] for k in my_issues],
            "disposition": ("pending"
                            if any(issues[k]["blocking"] for k in my_issues)
                            else "disposable"),
        })

    # 指纹只取规则版本与可复算的输入，不含时间
    fp_basis = {
        "matrix_version": matrix["version"],
        "matrix_fingerprint": matrix.get("_fingerprint"),
        "facility": facility,
        "containers": [_container_basis(by_id[i]) for i in ids],
    }
    result_fingerprint = sha_fp(fp_basis)

    return {
        "matrix_version": matrix["version"],
        "matrix_fingerprint": matrix.get("_fingerprint"),
        "disposition": disposition,
        "summary": {
            "containers": len(containers),
            "blockers": len(blocking),
            "warnings": len(warnings),
            "placed": sum(1 for cid in ids
                          if located[cid]["cabinet_id"] is not None
                          and not located[cid]["error"]),
            "unplaced": sum(1 for cid in ids
                            if located[cid]["cabinet_id"] is None),
        },
        "issues": issues,
        "warnings": warnings,
        "per_container": per_container,
        "calculation": {
            "limits": limits,
            "classification": {cid: inferred[cid]["classification"]
                               for cid in ids},
            "declared_check": {cid: {
                "declared": inferred[cid]["declared"],
                "conflicts": inferred[cid]["declared_conflicts"],
                "unknown": inferred[cid]["declared_unknown"],
            } for cid in ids},
            "pairs": pair_details,
            "merges": merge_details,
            "trays": tray_details,
        },
        "fingerprint": result_fingerprint,
        "fingerprint_basis": fp_basis,
    }


def _container_basis(c: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": c["id"],
        "components": c.get("components", []),
        "hazard_classes": c.get("hazard_classes", []),
        "material": c.get("material"),
        "capacity_l": c.get("capacity_l"),
        "volume_l": c.get("volume_l"),
        "position": c.get("position"),
        # 合并桶的禁配自检依赖叶级溯源，须纳入复算指纹输入
        "merge_provenance": c.get("merge_provenance"),
    }


# --------------------------------------------------------------------------- #
# 合并桶自检：源桶废液在同一容器内直接混合（比泄漏共置更严格），
# 依据合并前叶级溯源两两核查反应规则
# --------------------------------------------------------------------------- #
def _evaluate_merge(matrix: Dict[str, Any], target: Dict[str, Any],
                    provenance: List[Dict[str, Any]]) -> Dict[str, Any]:
    tid = target["id"]
    out: Dict[str, Any] = {
        "merged_container": tid,
        "target_position": target.get("position"),
        "source_containers": [p["source_id"] for p in provenance],
        "source_positions": {p["source_id"]: p.get("position")
                             for p in provenance},
        "source_volumes_l": {p["source_id"]: p.get("volume_l")
                             for p in provenance},
        "pair_checks": [],
        "_issues": [],
    }
    # 对每个叶级源桶按其合并前成分重新分类
    leaf_cls: Dict[str, Dict[str, Any]] = {}
    for p in provenance:
        leaf_cls[p["source_id"]] = classify_components(
            matrix, p.get("components", []))

    for i in range(len(provenance)):
        for j in range(i + 1, len(provenance)):
            pa, pb = provenance[i], provenance[j]
            ca, cb = leaf_cls[pa["source_id"]], leaf_cls[pb["source_id"]]
            check: Dict[str, Any] = {
                "sources": [pa["source_id"], pb["source_id"]],
                "positions": {pa["source_id"]: pa.get("position"),
                              pb["source_id"]: pb.get("position")},
                "matched_rules": [],
                "possible_rules": [],
                "violations": [],
            }
            matched: Dict[str, Dict[str, Any]] = {}
            for x, y in product(ca["definite"], cb["definite"]):
                r = M.find_reaction(matrix, x, y)
                if r:
                    matched[r["id"]] = {"rule_id": r["id"],
                                        "class_pair": [x, y],
                                        "definite": True}
            possible: Dict[str, Dict[str, Any]] = {}
            for x, y in product(ca["possible"], cb["possible"]):
                r = M.find_reaction(matrix, x, y)
                if r and r["id"] not in matched:
                    possible[r["id"]] = {"rule_id": r["id"],
                                         "class_pair": [x, y],
                                         "definite": False}
            check["matched_rules"] = list(matched.values())
            check["possible_rules"] = list(possible.values())

            rules = [(rid, next(x for x in matrix["reactions"]
                                if x["id"] == rid), True)
                     for rid in matched]
            rules += [(rid, next(x for x in matrix["reactions"]
                                 if x["id"] == rid), False)
                      for rid in possible]
            for rid, rule, definite in rules:
                # 桶内直接混合：任何反应规则都构成禁配（不区分共置层级）
                sev = rule["severity"]
                gas = f"，释放 {rule['gas']}" if rule.get("gas") else ""
                code = ("MERGE_INCOMPATIBLE" if definite
                        else "MERGE_POSSIBLY_INCOMPATIBLE")
                check["violations"].append({
                    "rule_id": rid, "definite": definite,
                    "severity": sev, "gas": rule.get("gas")})
                out["_issues"].append(_issue(
                    code, "blocker", "merge",
                    f"合并桶 {tid} 混合了禁配废液: 源桶 {pa['source_id']} 与 "
                    f"{pb['source_id']} 命中规则 {rid}（{sev}{gas}："
                    f"{rule.get('note', '')}）",
                    # 涉事容器含目标桶与两个源桶
                    [tid, pa["source_id"], pb["source_id"]],
                    {tid: target.get("position"),
                     pa["source_id"]: pa.get("position"),
                     pb["source_id"]: pb.get("position")},
                    {"rule_id": rid, "rule": rule, "definite": definite,
                     "merged_container": tid,
                     "source_pair": [pa["source_id"], pb["source_id"]],
                     "source_positions": {
                         pa["source_id"]: pa.get("position"),
                         pb["source_id"]: pb.get("position")},
                     "source_volumes_l": {
                         pa["source_id"]: pa.get("volume_l"),
                         pb["source_id"]: pb.get("volume_l")},
                     "mixed_context": "in_container"}))
            out["pair_checks"].append(check)
    return out


# --------------------------------------------------------------------------- #
# 成对反应
# --------------------------------------------------------------------------- #
def _evaluate_pair(matrix: Dict[str, Any], facility: Dict[str, Any],
                   spatial: Dict[str, Any],
                   a: Dict[str, Any], b: Dict[str, Any],
                   inf_a: Dict[str, Any], inf_b: Dict[str, Any],
                   loc_a: Dict[str, Any], loc_b: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "containers": [a["id"], b["id"]],
        "positions": {a["id"]: a.get("position"), b["id"]: b.get("position")},
        "matched_rules": [],
        "possible_rules": [],
        "colocation_level": None,
        "violations": [],
        "_issues": [],
    }
    cls_a = inf_a["classification"]
    cls_b = inf_b["classification"]

    # 用“确定类别”匹配规则
    matched: Dict[str, Dict[str, Any]] = {}
    for x, y in product(cls_a["definite"], cls_b["definite"]):
        r = M.find_reaction(matrix, x, y)
        if r:
            matched[r["id"]] = {"rule_id": r["id"], "class_pair": [x, y],
                                "definite": True}
    # 仅在可能类别中成立的规则（浓度范围高端才触发）
    possible: Dict[str, Dict[str, Any]] = {}
    for x, y in product(cls_a["possible"], cls_b["possible"]):
        r = M.find_reaction(matrix, x, y)
        if r and r["id"] not in matched:
            possible[r["id"]] = {"rule_id": r["id"], "class_pair": [x, y],
                                 "definite": False}

    # 规则冲突：同一类别对在矩阵中不可能（启动时 validate 拦截），
    # 这里检测同一桶对匹配到“可共置/禁共置”矛盾的多条规则
    out["matched_rules"] = list(matched.values())
    out["possible_rules"] = list(possible.values())
    if matched:
        policies = {r["colocation"] for rid in matched
                    for r in [next(x for x in matrix["reactions"]
                                   if x["id"] == rid)]}
        if M.COLOC_ALLOWED in policies and len(policies) > 1:
            out["_issues"].append(_issue(
                "RULE_CONFLICT", "blocker", "pair",
                f"{a['id']} 与 {b['id']} 同时命中允许与禁止共置的规则",
                [a["id"], b["id"]], out["positions"],
                {"matched_rules": out["matched_rules"]}))

    level = coloc_level(loc_a, loc_b)
    out["colocation_level"] = level
    if level is None or level == "separate_cabinet":
        return out
    if loc_a["error"] or loc_b["error"]:
        return out

    rules = [(rid, next(x for x in matrix["reactions"] if x["id"] == rid), True)
             for rid in matched]
    rules += [(rid, next(x for x in matrix["reactions"] if x["id"] == rid), False)
              for rid in possible]

    for rid, rule, definite in rules:
        violated = False
        reason = None
        # 共置层级核查
        if rule["colocation"] == M.COLOC_FORBID:
            if level in ("same_cabinet", "same_zone", "same_tray"):
                violated, reason = True, "规则要求同柜禁止，两桶在同一柜体"
        elif rule["colocation"] == M.COLOC_ZONE:
            if level in ("same_zone", "same_tray"):
                violated, reason = True, "规则要求禁同分区，两桶在同一分区"
            elif level == "same_cabinet":
                d = _zone_distance(facility, loc_a["cabinet_id"],
                                   loc_a["zone_id"], loc_b["zone_id"])
                if d is None:
                    violated, reason = True, (
                        "分区未提供坐标 position_xy，无法核验隔离距离 "
                        f"{rule['min_distance_m']}m，从严判待处置")
                elif d + EPS < rule["min_distance_m"]:
                    violated, reason = True, (
                        f"分区间距 {d:.2f}m < 要求 {rule['min_distance_m']}m")
                out.setdefault("distance_checks", []).append({
                    "rule_id": rid, "distance_m": None if d is None else round(d, 4),
                    "required_m": rule["min_distance_m"],
                    "ok": not violated})
        elif rule["colocation"] == M.COLOC_TRAY and level == "same_tray":
            violated, reason = True, "规则要求禁同托盘，两桶共用同一托盘"

        # 同柜通风附加要求
        vent_note = None
        if rule.get("same_ventilation") and level != "separate_cabinet":
            have = spatial["cabinets"][loc_a["cabinet_id"]]["ventilation"]
            ok, gap = _vent_ok(matrix, have, rule["same_ventilation"])
            vent_note = {"rule_id": rid, "required": rule["same_ventilation"],
                         "actual": have, "ok": ok, "level_gap": gap}
            out.setdefault("ventilation_checks", []).append(vent_note)
            if not ok:
                violated = True
                reason = (reason + "；" if reason else "") + (
                    f"同柜通风 {have} 不满足规则要求 "
                    f"{rule['same_ventilation']}")

        if violated:
            sev = rule["severity"]
            gas = f"（{rule['gas']}）" if rule.get("gas") else ""
            code = ("INCOMPATIBLE_COLOCATION" if definite
                    else "POSSIBLE_INCOMPATIBLE_COLOCATION")
            out["violations"].append({
                "rule_id": rid, "definite": definite, "reason": reason,
                "severity": sev, "gas": rule.get("gas")})
            out["_issues"].append(_issue(
                code, "blocker", "pair",
                f"{'确定' if definite else '可能（浓度范围高端）'}禁配: "
                f"{a['id']} 与 {b['id']} {sev}{gas} 共置于 {level}；{reason}。"
                f"依据 {rid}: {rule.get('note', '')}",
                [a["id"], b["id"]], out["positions"],
                {"rule_id": rid, "rule": rule, "definite": definite,
                 "colocation_level": level, "reason": reason,
                 "ventilation": vent_note}))
    return out


# --------------------------------------------------------------------------- #
# 托盘盛漏
# --------------------------------------------------------------------------- #
def _evaluate_trays(matrix: Dict[str, Any], spatial: Dict[str, Any],
                    containers: Sequence[Dict[str, Any]],
                    located: Dict[str, Dict[str, Any]]
                    ) -> List[Dict[str, Any]]:
    factor = matrix["limits"].get("tray_usable_factor", 1.0)
    ratio_req = matrix["limits"]["tray_capacity_ratio"]

    # tray_id -> 容器列表（只统计位置有效的）
    load: Dict[str, List[Dict[str, Any]]] = {}
    for c in containers:
        loc = located[c["id"]]
        if loc["tray_id"] and not loc["error"]:
            load.setdefault(loc["tray_id"], []).append(c)

    details: List[Dict[str, Any]] = []
    for cab_id, cab in spatial["cabinets"].items():
        for tray_id, tray in cab["trays"].items():
            members = load.get(tray_id, [])
            try:
                vol = sum(float(m["volume_l"]) for m in members)
            except (TypeError, ValueError):
                vol = 0.0
            try:
                nominal = float(tray.get("capacity_l", 0))
            except (TypeError, ValueError):
                nominal = 0.0
            try:
                usable = (float(tray["usable_capacity_l"])
                          if tray.get("usable_capacity_l") is not None
                          else nominal * factor)
            except (TypeError, ValueError):
                usable = nominal * factor
            required = vol * ratio_req
            margin = usable - required
            td = {
                "tray_id": tray_id,
                "cabinet_id": cab_id,
                "zone_id": spatial["tray_zone"][tray_id],
                "nominal_capacity_l": nominal,
                "usable_capacity_l": round(usable, 6),
                "usable_factor": factor,
                "containers": [m["id"] for m in members],
                "total_volume_l": round(vol, 6),
                "capacity_ratio_required": ratio_req,
                "required_containment_l": round(required, 6),
                "margin_l": round(margin, 6),
                "sufficient": margin >= -EPS,
                "_issues": [],
            }
            if members and margin < -EPS:
                td["_issues"].append(_issue(
                    "TRAY_CONTAINMENT_INSUFFICIENT", "blocker", "tray",
                    f"托盘 {tray_id} 盛漏量不足: 有效 {usable:.1f}L < "
                    f"所载 {vol:.1f}L × {ratio_req} = {required:.1f}L"
                    f"（缺口 {-margin:.1f}L）",
                    [m["id"] for m in members],
                    {m["id"]: m.get("position") for m in members},
                    {"tray_id": tray_id,
                     "usable_capacity_l": round(usable, 6),
                     "total_volume_l": round(vol, 6),
                     "required_containment_l": round(required, 6),
                     "deficit_l": round(-margin, 6),
                     "ratio_required": ratio_req}))
            details.append(td)
    return details


# --------------------------------------------------------------------------- #
# 合并：两桶废液混装到目标桶（浓度按体积加权重算，保留范围）
# --------------------------------------------------------------------------- #
def merge_components(sources: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """体积加权混合：各成分浓度区间按体积守恒传播。

    成分仅在某一桶存在时，按该桶体积占比稀释；两桶都存在时区间线性组合。
    """
    total = sum(float(s["volume_l"]) for s in sources)
    if total <= 0:
        raise ValueError("合并源桶总装量为 0，无法按体积加权计算混合浓度")
    acc: Dict[str, List[Tuple[float, float, float]]] = {}
    for s in sources:
        v = float(s["volume_l"])
        for comp in s.get("components", []):
            name = _norm_comp_name(comp["name"])
            lo = float(comp["conc_min"]) * v
            hi = float(comp["conc_max"]) * v
            acc.setdefault(name, []).append((v, lo, hi))
    out: List[Dict[str, Any]] = []
    for name, parts in sorted(acc.items()):
        lo = sum(p[1] for p in parts) / total
        hi = sum(p[2] for p in parts) / total
        out.append({"name": name,
                    "conc_min": round(lo, 6), "conc_max": round(hi, 6)})
    return out


def merge_containers(target: Dict[str, Any],
                     sources: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    vol = sum(float(s["volume_l"]) for s in sources)
    declared = sorted({h for s in sources for h in (s.get("hazard_classes") or [])})
    return {
        "id": target["id"],
        "components": merge_components(sources),
        "hazard_classes": declared,
        "material": target.get("material"),
        "capacity_l": target.get("capacity_l"),
        "volume_l": round(vol, 6),
        "position": target.get("position"),
    }


# --------------------------------------------------------------------------- #
# 版本比较：矩阵 diff 与 修订结果 diff
# --------------------------------------------------------------------------- #
def _issue_key(iss: Dict[str, Any]) -> str:
    rule = iss.get("basis", {}).get("rule_id", "")
    return "|".join([iss["code"], rule,
                     ",".join(sorted(iss["containers"]))])


def diff_results(old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    """比较两次复核结果（修订）。"""
    old_map = {_issue_key(x): x for x in old.get("issues", [])}
    new_map = {_issue_key(x): x for x in new.get("issues", [])}
    resolved = sorted(set(old_map) - set(new_map))
    introduced = sorted(set(new_map) - set(old_map))
    persisted = sorted(set(old_map) & set(new_map))

    def brief(iss: Dict[str, Any]) -> Dict[str, Any]:
        return {"code": iss["code"], "message": iss["message"],
                "containers": iss["containers"], "rule":
                iss.get("basis", {}).get("rule_id")}

    old_pc = {x["container_id"]: x for x in old.get("per_container", [])}
    new_pc = {x["container_id"]: x for x in new.get("per_container", [])}
    container_changes = []
    for cid in sorted(set(old_pc) | set(new_pc)):
        o, n = old_pc.get(cid), new_pc.get(cid)
        chg: Dict[str, Any] = {"container_id": cid}
        if o is None:
            chg["change"] = "added"
            chg["new"] = n
        elif n is None:
            chg["change"] = "removed"
            chg["old"] = o
        else:
            diffs = {}
            for field in ("position", "hazard_definite", "hazard_possible",
                          "ambiguous", "fill_ratio", "material",
                          "disposition", "issues"):
                if o.get(field) != n.get(field):
                    diffs[field] = {"old": o.get(field), "new": n.get(field)}
            if diffs:
                chg["change"] = "modified"
                chg["fields"] = diffs
            else:
                chg["change"] = "unchanged"
        container_changes.append(chg)

    return {
        "matrix_version": {"old": old.get("matrix_version"),
                           "new": new.get("matrix_version")},
        "fingerprint": {"old": old.get("fingerprint"),
                        "new": new.get("fingerprint")},
        "disposition": {"old": old.get("disposition"),
                        "new": new.get("disposition"),
                        "changed": old.get("disposition") != new.get("disposition")},
        "issues_resolved": [brief(old_map[k]) for k in resolved],
        "issues_introduced": [brief(new_map[k]) for k in introduced],
        "issues_persisted": [brief(new_map[k]) for k in persisted],
        "summary": {"old": old.get("summary"), "new": new.get("summary")},
        "container_changes": container_changes,
    }


def diff_matrices(old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    """比较两版冻结矩阵的规则差异。"""
    def reactions_idx(m):
        return {r["id"]: r for r in m["reactions"]}

    ro, rn = reactions_idx(old), reactions_idx(new)
    added = [rn[k] for k in sorted(set(rn) - set(ro))]
    removed = [ro[k] for k in sorted(set(ro) - set(rn))]
    changed = []
    for k in sorted(set(ro) & set(rn)):
        if canonical(ro[k]) != canonical(rn[k]):
            fields = {}
            for f in set(ro[k]) | set(rn[k]):
                if ro[k].get(f) != rn[k].get(f):
                    fields[f] = {"old": ro[k].get(f), "new": rn[k].get(f)}
            changed.append({"rule_id": k, "fields": fields})

    limits_changed = {k: {"old": old["limits"].get(k), "new": new["limits"].get(k)}
                      for k in set(old["limits"]) | set(new["limits"])
                      if old["limits"].get(k) != new["limits"].get(k)}

    comp_changed = {}
    for name in sorted(set(old.get("component_class", {}))
                       | set(new.get("component_class", {}))):
        if canonical(old.get("component_class", {}).get(name)) != \
                canonical(new.get("component_class", {}).get(name)):
            comp_changed[name] = {
                "old": old.get("component_class", {}).get(name),
                "new": new.get("component_class", {}).get(name)}

    vent_changed = {k: {"old": old.get("class_ventilation", {}).get(k),
                        "new": new.get("class_ventilation", {}).get(k)}
                    for k in set(old.get("class_ventilation", {}))
                    | set(new.get("class_ventilation", {}))
                    if old.get("class_ventilation", {}).get(k)
                    != new.get("class_ventilation", {}).get(k)}

    mat_changed = {k: {"old": old.get("material_compat", {}).get(k),
                       "new": new.get("material_compat", {}).get(k)}
                   for k in set(old.get("material_compat", {}))
                   | set(new.get("material_compat", {}))
                   if old.get("material_compat", {}).get(k)
                   != new.get("material_compat", {}).get(k)}

    return {
        "versions": {"old": old["version"], "new": new["version"]},
        "limits_changed": limits_changed,
        "reactions_added": added,
        "reactions_removed": removed,
        "reactions_changed": changed,
        "component_thresholds_changed": comp_changed,
        "ventilation_changed": vent_changed,
        "material_compat_changed": mat_changed,
        "has_changes": bool(limits_changed or added or removed or changed
                            or comp_changed or vent_changed or mat_changed),
    }
