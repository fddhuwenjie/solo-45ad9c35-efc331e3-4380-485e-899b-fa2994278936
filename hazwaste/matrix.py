"""版本化相容性矩阵。

矩阵在“冻结”之后内容不可再变：存储层只允许冻结版本存在，评估端点拿到的
matrix_version 必须能在库中找到对应冻结版本，从而保证试排 / 预检 / 确认 /
版本比较共用同一份冻结规则。

v1.0 内置在服务启动时自动入库（幂等）。v2.0 演示“矩阵升级后旧结果仍可复核、
版本比较可指出规则差异”。
"""
from __future__ import annotations

import hashlib
from copy import deepcopy
from typing import Any, Dict, List, Optional, Tuple

# 危害类别
ACID = "acid"                 # 酸
BASE = "base"                 # 碱
CYANIDE = "cyanide"           # 含氰
SULFIDE = "sulfide"           # 硫化物
OXIDIZER = "oxidizer"         # 氧化剂
ORGANIC = "organic"           # 有机（可燃）
HALOGEN = "halogen"           # 卤素（次卤酸盐等）
HEAVY_METAL = "heavy_metal"   # 重金属（一般危害，反应性弱）
TOXIC = "toxic"               # 有毒（一般毒性，无特殊反应性）
CORROSIVE = "corrosive"       # 一般腐蚀性

HAZARD_CLASSES = [
    ACID, BASE, CYANIDE, SULFIDE, OXIDIZER, ORGANIC,
    HALOGEN, HEAVY_METAL, TOXIC, CORROSIVE,
]

# 反应严重度
SEV_GAS = "toxic_gas"         # 放出有毒气体
SEV_VIOLENT = "violent"       # 剧烈反应
SEV_HEAT = "heat"             # 放热
SEV_MODERATE = "moderate"     # 一般不相容
SEVERITY_ORDER = [SEV_GAS, SEV_VIOLENT, SEV_HEAT, SEV_MODERATE]

# 共置判定层级（数值越小要求越严）
COLOC_FORBID = "forbidden"    # 同柜禁止
COLOC_ZONE = "same_zone"      # 仅禁同分区（同柜可，须留隔离距离）
COLOC_TRAY = "same_tray"      # 仅禁同托盘
COLOC_ALLOWED = "allowed"     # 可共置
COLOC_ORDER = [COLOC_FORBID, COLOC_ZONE, COLOC_TRAY, COLOC_ALLOWED]


def matrix_fingerprint(matrix: Dict[str, Any]) -> str:
    """矩阵内容指纹（canonical JSON + sha256），冻结后与版本号绑定。"""
    canonical = _canonical(matrix)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _canonical(obj: Any) -> str:
    import json

    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _re(rid: str, a: str, b: str, severity: str, gas: Optional[str] = None,
        coloc: str = COLOC_FORBID, min_distance: float = 0.0,
        same_ventilation: Optional[str] = None, note: str = "") -> Dict[str, Any]:
    """构造一条反应规则。

    coloc      : 共置策略。forbidden 同柜禁止；same_zone 只禁同分区；
                 same_tray 只禁同托盘。
    min_distance: coloc != forbidden 时，同柜不同分区要求的最小隔离距离(m)。
    same_ventilation: 同柜时对通风方式的额外要求。
    """
    return {
        "id": rid,
        "pair": sorted([a, b]),
        "severity": severity,
        "gas": gas,
        "colocation": coloc,
        "min_distance_m": float(min_distance),
        "same_ventilation": same_ventilation,
        "note": note,
    }


def build_v1() -> Dict[str, Any]:
    """v1.0 基线矩阵。"""
    m: Dict[str, Any] = {
        "schema": "compatibility-matrix",
        "version": "1.0",
        "description": "危险废液暂存相容性基线矩阵",
        "limits": {
            # 装填率上限（体积占容器公称容量）
            "max_fill_ratio": 0.90,
            # 盛漏托盘有效容积至少为所载容器总装量的该倍数
            "tray_capacity_ratio": 1.10,
            # 托盘在未声明有效容积时，公称容积按该折减系数计算
            "tray_usable_factor": 0.90,
        },
        # 成分名（小写规范化后）-> 浓度区间(%) 可推得的危害类别，按严重度优先
        "component_class": {
            "hcl":            [[0.0, 100.0, [ACID, CORROSIVE]]],
            "h2so4":          [[0.0, 100.0, [ACID, CORROSIVE]]],
            "hno3":           [[0.0, 100.0, [ACID, CORROSIVE, OXIDIZER]]],
            "nitric_acid":    [[0.0, 100.0, [ACID, CORROSIVE, OXIDIZER]]],
            "acetic_acid":    [[0.0, 100.0, [ACID, CORROSIVE]]],
            "naoh":           [[0.0, 100.0, [BASE, CORROSIVE]]],
            "koh":            [[0.0, 100.0, [BASE, CORROSIVE]]],
            # 氰化物：>=1% 即按含氰管控；低浓度仅按有毒
            "nacn":           [[0.0, 1.0, [TOXIC]], [1.0, 100.0, [CYANIDE, TOXIC]]],
            "kcn":            [[0.0, 1.0, [TOXIC]], [1.0, 100.0, [CYANIDE, TOXIC]]],
            "cyanide":        [[0.0, 1.0, [TOXIC]], [1.0, 100.0, [CYANIDE, TOXIC]]],
            # 硫化物：>=5% 按硫化物（遇酸放 H2S）
            "na2s":           [[0.0, 5.0, [TOXIC]], [5.0, 100.0, [SULFIDE, TOXIC]]],
            "sulfide":        [[0.0, 5.0, [TOXIC]], [5.0, 100.0, [SULFIDE, TOXIC]]],
            # 过氧化氢：>=27.5% 为强氧化剂（与有机物剧烈反应），低浓度一般危害
            "h2o2":           [[0.0, 8.0, [TOXIC]],
                               [8.0, 27.5, [OXIDIZER]],
                               [27.5, 100.0, [OXIDIZER]]],
            "hydrogen_peroxide": [[0.0, 8.0, [TOXIC]],
                                  [8.0, 27.5, [OXIDIZER]],
                                  [27.5, 100.0, [OXIDIZER]]],
            # 铬酸洗液 / 重铬酸盐按氧化剂
            "chromic_acid":   [[0.0, 100.0, [ACID, CORROSIVE, OXIDIZER]]],
            "dichromate":     [[0.0, 100.0, [OXIDIZER, TOXIC]]],
            "permanganate":   [[0.0, 100.0, [OXIDIZER, TOXIC]]],
            "hypochlorite":   [[0.0, 100.0, [HALOGEN, OXIDIZER, CORROSIVE]]],
            "bleach":         [[0.0, 100.0, [HALOGEN, OXIDIZER, CORROSIVE]]],
            # 有机溶剂 / 有机废液：按浓度，>=10% 即有机可燃
            "ethanol":        [[0.0, 10.0, [TOXIC]], [10.0, 100.0, [ORGANIC]]],
            "methanol":       [[0.0, 10.0, [TOXIC]], [10.0, 100.0, [ORGANIC]]],
            "acetone":        [[0.0, 10.0, [TOXIC]], [10.0, 100.0, [ORGANIC]]],
            "toluene":        [[0.0, 10.0, [TOXIC]], [10.0, 100.0, [ORGANIC]]],
            "xylene":         [[0.0, 10.0, [TOXIC]], [10.0, 100.0, [ORGANIC]]],
            "organic_solvent":[[0.0, 10.0, [TOXIC]], [10.0, 100.0, [ORGANIC]]],
            "organic_waste":  [[0.0, 10.0, [TOXIC]], [10.0, 100.0, [ORGANIC]]],
            "solvent_waste":  [[0.0, 10.0, [TOXIC]], [10.0, 100.0, [ORGANIC]]],
            "oil":            [[0.0, 10.0, [TOXIC]], [10.0, 100.0, [ORGANIC]]],
            # 重金属废液
            "lead":           [[0.0, 100.0, [HEAVY_METAL, TOXIC]]],
            "mercury":        [[0.0, 100.0, [HEAVY_METAL, TOXIC]]],
            "cadmium":        [[0.0, 100.0, [HEAVY_METAL, TOXIC]]],
            "chromium":       [[0.0, 100.0, [HEAVY_METAL, TOXIC]]],
            "heavy_metal":    [[0.0, 100.0, [HEAVY_METAL, TOXIC]]],
        },
        # 材质适配：材质 -> 不兼容类别
        "material_compat": {
            "hdpe":         [],
            "pp":           [],
            "glass":        [BASE],
            "stainless_steel": [ACID, HALOGEN],
            "steel":        [ACID, BASE, HALOGEN, CORROSIVE],
            "pvc":          [ORGANIC],
        },
        "material_alias": {
            "pe": "hdpe", "聚乙烯": "hdpe",
            "聚丙烯": "pp",
            "玻璃": "glass",
            "不锈钢": "stainless_steel",
            "碳钢": "steel", "钢": "steel",
        },
        # 通风方式：关键
        # 局部排风/防爆排风可承担明火/气体风险；一般机械通风不可
        "ventilation_levels": {
            "none": 0,
            "natural": 1,
            "mechanical": 2,
            "local_exhaust": 3,
            "explosion_proof": 4,
        },
        # 类别单独存放时的通风要求
        "class_ventilation": {
            ORGANIC: ("explosion_proof", "有机废液须防爆排风"),
            CYANIDE: ("local_exhaust", "含氰废液须局部排风"),
            SULFIDE: ("local_exhaust", "硫化物废液须局部排风"),
            OXIDIZER: ("mechanical", "氧化剂废液须机械通风"),
            ACID: ("mechanical", "酸性废液须机械通风"),
            HALOGEN: ("local_exhaust", "次卤酸盐废液须局部排风"),
        },
        "reactions": [
            _re("R1", ACID, CYANIDE, SEV_GAS, gas="HCN",
                coloc=COLOC_FORBID,
                note="酸遇含氰废液释放氰化氢剧毒气体"),
            _re("R2", ACID, SULFIDE, SEV_GAS, gas="H2S",
                coloc=COLOC_FORBID,
                note="酸遇硫化物释放硫化氢剧毒气体"),
            _re("R3", ACID, BASE, SEV_HEAT,
                coloc=COLOC_ZONE, min_distance=1.0,
                same_ventilation="mechanical",
                note="酸碱中和放热，禁止同分区，同柜须相距≥1m 且机械通风"),
            _re("R4", OXIDIZER, ORGANIC, SEV_VIOLENT,
                coloc=COLOC_FORBID,
                note="强氧化剂与有机废液可剧烈反应/燃烧爆炸"),
            _re("R5", OXIDIZER, BASE, SEV_MODERATE,
                coloc=COLOC_TRAY,
                note="氧化剂与碱不宜同托盘"),
            _re("R6", HALOGEN, ACID, SEV_GAS, gas="Cl2",
                coloc=COLOC_FORBID,
                note="次卤酸盐遇强酸可释放氯气"),
            _re("R7", HALOGEN, ORGANIC, SEV_VIOLENT,
                coloc=COLOC_FORBID,
                note="次卤酸盐与有机物可剧烈反应"),
        ],
    }
    return m


def build_v2() -> Dict[str, Any]:
    """v2.0：收紧参数并新增规则（用于版本比较与矩阵升级）。

    变化点：
    - max_fill_ratio 0.90 -> 0.85；tray_capacity_ratio 1.10 -> 1.25
    - 硫化物阈值 5% -> 1%；新增酸+次卤酸盐 min_distance 说明调整
    - 新增 R8：酸+重金属（含铬废液）放热/有毒雾，禁同分区
    - R3 隔离距离 1.0 -> 1.5m
    """
    m = deepcopy(build_v1())
    m["version"] = "2.0"
    m["description"] = "危险废液暂存相容性矩阵（收紧版）"
    m["limits"]["max_fill_ratio"] = 0.85
    m["limits"]["tray_capacity_ratio"] = 1.25
    m["component_class"]["sulfide"] = [
        [0.0, 1.0, [TOXIC]], [1.0, 100.0, [SULFIDE, TOXIC]]
    ]
    m["component_class"]["na2s"] = [
        [0.0, 1.0, [TOXIC]], [1.0, 100.0, [SULFIDE, TOXIC]]
    ]
    for r in m["reactions"]:
        if r["id"] == "R3":
            r["min_distance_m"] = 1.5
            r["note"] = "酸碱中和放热，禁止同分区，同柜须相距≥1.5m 且机械通风"
    m["reactions"].append(
        _re("R8", ACID, HEAVY_METAL, SEV_HEAT,
            coloc=COLOC_ZONE, min_distance=1.0,
            same_ventilation="mechanical",
            note="酸混入含铬等重金属废液可放热并产生有毒酸雾")
    )
    return m


BUILTIN = {"1.0": build_v1, "2.0": build_v2}


def normalize_material(matrix: Dict[str, Any], material: str) -> str:
    aliases = matrix.get("material_alias", {})
    key = (material or "").strip().lower()
    return aliases.get(key, key)


def find_reaction(matrix: Dict[str, Any], a: str, b: str) -> Optional[Dict[str, Any]]:
    pair = sorted([a, b])
    for r in matrix["reactions"]:
        if r["pair"] == pair:
            return r
    return None


def validate_matrix(matrix: Dict[str, Any]) -> List[str]:
    """静态自检：规则冲突 / 结构错误。返回问题说明列表（空=通过）。"""
    errs: List[str] = []
    seen: Dict[Tuple[str, str], str] = {}
    for r in matrix.get("reactions", []):
        key = tuple(sorted(r["pair"]))
        if key in seen:
            errs.append(f"反应规则冲突: {key} 同时被 {seen[key]} 与 {r['id']} 定义")
        seen[key] = r["id"]
        if r["colocation"] != COLOC_FORBID and r["min_distance_m"] < 0:
            errs.append(f"规则 {r['id']} 隔离距离为负")
        for c in r["pair"]:
            if c not in HAZARD_CLASSES:
                errs.append(f"规则 {r['id']} 引用未知危害类别 {c}")
    lim = matrix.get("limits", {})
    if not 0 < lim.get("max_fill_ratio", 0) <= 1:
        errs.append("max_fill_ratio 必须在 (0,1]")
    if lim.get("tray_capacity_ratio", 0) < 1.0:
        errs.append("tray_capacity_ratio 必须 >=1.0")
    return errs
