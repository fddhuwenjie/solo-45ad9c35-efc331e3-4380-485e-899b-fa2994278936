"""引擎与存储层单元测试。"""
import copy
import json
import unittest

from hazwaste import engine, matrix as M
from hazwaste.storage import Store, StoreError


def facility():
    return {
        "id": "F1",
        "cabinets": [
            {"id": "CAB-A", "ventilation": "mechanical", "zones": [
                {"id": "Z-acid", "position_xy": [0.0, 0.0], "trays": [
                    {"id": "T-a1", "capacity_l": 60},
                    {"id": "T-a2", "capacity_l": 200}]},
                {"id": "Z-cyan", "position_xy": [0.4, 0.0], "trays": [
                    {"id": "T-c1", "capacity_l": 60}]},
                {"id": "Z-other", "position_xy": [3.0, 0.0], "trays": [
                    {"id": "T-o1", "capacity_l": 60}]}]},
            {"id": "CAB-B", "ventilation": "explosion_proof", "zones": [
                {"id": "Z-org", "position_xy": [0.0, 0.0], "trays": [
                    {"id": "T-g1", "capacity_l": 120}]}]},
        ],
    }


def acid(cid="A1", tray="T-a1", zone="Z-acid", cab="CAB-A", vol=20):
    return {"id": cid,
            "components": [{"name": "hcl", "conc_min": 30, "conc_max": 32}],
            "hazard_classes": ["acid"],
            "material": "hdpe", "capacity_l": 25, "volume_l": vol,
            "position": {"cabinet_id": cab, "zone_id": zone, "tray_id": tray}}


def cyanide(cid="K1", tray="T-c1", zone="Z-cyan", cab="CAB-A", vol=20):
    return {"id": cid,
            "components": [{"name": "nacn", "conc_min": 5, "conc_max": 8}],
            "hazard_classes": ["cyanide"],
            "material": "hdpe", "capacity_l": 25, "volume_l": vol,
            "position": {"cabinet_id": cab, "zone_id": zone, "tray_id": tray}}


class ClassificationTests(unittest.TestCase):
    def setUp(self):
        self.m = M.build_v1()

    def test_crossing_band_is_ambiguous(self):
        # NaCN 0.5%~2% 跨越 1% 阈值 -> toxic 确定, cyanide 可能
        out = engine.classify_components(
            self.m, [{"name": "nacn", "conc_min": 0.5, "conc_max": 2.0}])
        self.assertTrue(out["ambiguous"])
        self.assertIn("toxic", out["definite"])
        self.assertIn("cyanide", out["possible"])
        self.assertNotIn("cyanide", out["definite"])

    def test_full_inside_high_band_definite(self):
        out = engine.classify_components(
            self.m, [{"name": "nacn", "conc_min": 2, "conc_max": 5}])
        self.assertFalse(out["ambiguous"])
        self.assertIn("cyanide", out["definite"])

    def test_unknown_component(self):
        out = engine.classify_components(
            self.m, [{"name": "mystery_x", "conc_min": 1, "conc_max": 2}])
        self.assertEqual(out["unknown_components"], ["mystery_x"])

    def test_point_concentration_hits_band(self):
        # 缺陷回归：HCl [30,30] 点浓度必须命中唯一阈值带，
        # 确定类别为 acid+corrosive，且不产生多结论
        out = engine.classify_components(
            self.m, [{"name": "hcl", "conc_min": 30, "conc_max": 30}])
        self.assertEqual(out["definite"], ["acid", "corrosive"])
        self.assertEqual(out["possible"], ["acid", "corrosive"])
        self.assertFalse(out["ambiguous"])
        self.assertEqual(len(out["band_hits"][0]["bands"]), 1)
        hit = out["band_hits"][0]["bands"][0]
        self.assertTrue(hit["point_concentration"])
        self.assertTrue(hit["covers_full_range"])

    def test_point_concentration_on_threshold_is_strict(self):
        # 点浓度恰落在 1% 阈值边界 -> 相邻两带都命中，按多结论从严
        out = engine.classify_components(
            self.m, [{"name": "nacn", "conc_min": 1.0, "conc_max": 1.0}])
        self.assertTrue(out["ambiguous"])
        self.assertIn("cyanide", out["possible"])

    def test_point_concentration_layout(self):
        # 端到端：点浓度酸桶应被正确分类，单独合规存放时可处置
        fac = facility()
        a = acid()
        a["components"] = [{"name": "hcl", "conc_min": 30, "conc_max": 30}]
        res = engine.evaluate_layout(self.m, fac, [a])
        self.assertEqual(res["disposition"], "disposable",
                         msg=json.dumps(res["issues"], ensure_ascii=False))
        pc = res["calculation"]["classification"]["A1"]
        self.assertEqual(pc["definite"], ["acid", "corrosive"])


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.m = M.build_v1()
        self.fac = facility()

    def issues_by_code(self, result):
        return [i["code"] for i in result["issues"]]

    def test_acid_cyanide_same_cabinet_forbidden(self):
        res = engine.evaluate_layout(self.m, self.fac,
                                     [acid(), cyanide()])
        self.assertEqual(res["disposition"], "pending")
        codes = self.issues_by_code(res)
        self.assertIn("INCOMPATIBLE_COLOCATION", codes)
        issue = next(i for i in res["issues"]
                     if i["code"] == "INCOMPATIBLE_COLOCATION")
        self.assertEqual(issue["basis"]["rule_id"], "R1")
        self.assertEqual(issue["basis"]["rule"]["gas"], "HCN")
        self.assertEqual(issue["containers"], ["A1", "K1"])
        # 位置必须随问题返回
        self.assertEqual(issue["positions"]["A1"]["tray_id"], "T-a1")

    def test_separate_cabinets_ok_for_acid_cyanide(self):
        k = cyanide(cab="CAB-B", zone="Z-org", tray="T-g1")
        # CAB-B 防爆排风（等级4）满足含氰的局部排风要求（等级3）
        res = engine.evaluate_layout(self.m, self.fac, [acid(), k])
        self.assertNotIn("INCOMPATIBLE_COLOCATION", self.issues_by_code(res))
        self.assertEqual(res["disposition"], "disposable",
                         msg=json.dumps(res["issues"], ensure_ascii=False))

    def test_oxidizer_organic_violent(self):
        ox = {"id": "OX1",
              "components": [{"name": "h2o2", "conc_min": 30, "conc_max": 35}],
              "hazard_classes": ["oxidizer"], "material": "hdpe",
              "capacity_l": 25, "volume_l": 20,
              "position": {"cabinet_id": "CAB-B", "zone_id": "Z-org",
                           "tray_id": "T-g1"}}
        org = {"id": "ORG1",
               "components": [{"name": "acetone", "conc_min": 60,
                               "conc_max": 80}],
               "hazard_classes": ["organic"], "material": "hdpe",
               "capacity_l": 25, "volume_l": 20,
               "position": {"cabinet_id": "CAB-B", "zone_id": "Z-org",
                            "tray_id": "T-g1"}}
        res = engine.evaluate_layout(self.m, self.fac, [ox, org])
        self.assertEqual(res["disposition"], "pending")
        iss = [i for i in res["issues"]
               if i["code"] == "INCOMPATIBLE_COLOCATION"]
        self.assertTrue(iss)
        self.assertEqual(iss[0]["basis"]["rule_id"], "R4")
        self.assertEqual(iss[0]["basis"]["rule"]["severity"], "violent")

    def test_possible_incompatible_from_range(self):
        # 酸桶 + 浓度范围高端才可能含氰的桶，同柜 -> 可能禁配
        maybe_cyan = {"id": "K2",
                      "components": [{"name": "nacn", "conc_min": 0.5,
                                      "conc_max": 2}],
                      "hazard_classes": [], "material": "hdpe",
                      "capacity_l": 25, "volume_l": 20,
                      "position": {"cabinet_id": "CAB-A", "zone_id": "Z-cyan",
                                   "tray_id": "T-c1"}}
        res = engine.evaluate_layout(self.m, self.fac, [acid(), maybe_cyan])
        codes = self.issues_by_code(res)
        self.assertIn("CLASS_AMBIGUOUS", codes)
        self.assertIn("POSSIBLE_INCOMPATIBLE_COLOCATION", codes)
        self.assertEqual(res["disposition"], "pending")

    def test_material_incompatible_glass_with_base(self):
        base = {"id": "B1",
                "components": [{"name": "naoh", "conc_min": 20,
                                "conc_max": 30}],
                "hazard_classes": ["base"], "material": "glass",
                "capacity_l": 25, "volume_l": 20,
                "position": {"cabinet_id": "CAB-A", "zone_id": "Z-other",
                             "tray_id": "T-o1"}}
        res = engine.evaluate_layout(self.m, self.fac, [base])
        self.assertIn("MATERIAL_INCOMPATIBLE", self.issues_by_code(res))

    def test_fill_ratio(self):
        over = acid(vol=24.5)  # 24.5/25 = 0.98 > 0.90
        res = engine.evaluate_layout(self.m, self.fac, [over])
        iss = next(i for i in res["issues"] if i["code"] == "FILL_OVERFLOW")
        self.assertAlmostEqual(iss["basis"]["fill_ratio"], 0.98)
        self.assertEqual(iss["basis"]["limit"], 0.90)

    def test_tray_containment_insufficient(self):
        # T-a1 公称 60L -> 有效 54L；两桶 50L 需 50*1.1=55L -> 不足
        a1 = acid(cid="A1", vol=25)
        a2 = acid(cid="A2", vol=25)
        res = engine.evaluate_layout(self.m, self.fac, [a1, a2])
        iss = next(i for i in res["issues"]
                   if i["code"] == "TRAY_CONTAINMENT_INSUFFICIENT")
        self.assertEqual(iss["containers"], ["A1", "A2"])
        self.assertAlmostEqual(iss["basis"]["deficit_l"], 1.0)
        self.assertEqual(iss["positions"]["A1"]["tray_id"], "T-a1")

    def test_tray_containment_ok(self):
        # 20L -> 需 22L，T-a1 有效 54L 足够
        res = engine.evaluate_layout(self.m, self.fac, [acid()])
        self.assertNotIn("TRAY_CONTAINMENT_INSUFFICIENT",
                         self.issues_by_code(res))

    def test_distance_enforcement(self):
        # 酸碱 R3：同柜不同分区，Z-acid(0,0) 与 Z-cyan(0.4,0) 相距 0.4m < 1m
        b = {"id": "B1",
             "components": [{"name": "naoh", "conc_min": 20, "conc_max": 30}],
             "hazard_classes": ["base"], "material": "hdpe",
             "capacity_l": 25, "volume_l": 20,
             "position": {"cabinet_id": "CAB-A", "zone_id": "Z-cyan",
                          "tray_id": "T-c1"}}
        res = engine.evaluate_layout(self.m, self.fac, [acid(), b])
        iss = [i for i in res["issues"]
               if i["code"] == "INCOMPATIBLE_COLOCATION"]
        self.assertTrue(any(i["basis"]["rule_id"] == "R3" for i in iss))
        reason = next(i for i in iss if i["basis"]["rule_id"] == "R3")
        self.assertIn("0.40m", reason["message"])
        # 放到 3m 外的 Z-other 后距离满足，但 R3 还要求 mechanical 通风——满足
        b2 = dict(b, position={"cabinet_id": "CAB-A", "zone_id": "Z-other",
                               "tray_id": "T-o1"})
        res2 = engine.evaluate_layout(self.m, self.fac, [acid(), b2])
        r3 = [i for i in res2["issues"]
              if i["code"] == "INCOMPATIBLE_COLOCATION"
              and i["basis"]["rule_id"] == "R3"]
        self.assertEqual(r3, [])

    def test_ventilation_requirement(self):
        # 有机桶在机械通风柜 CAB-A（有机须防爆排风）
        org = {"id": "ORG2",
               "components": [{"name": "toluene", "conc_min": 60,
                               "conc_max": 70}],
               "hazard_classes": ["organic"], "material": "hdpe",
               "capacity_l": 25, "volume_l": 20,
               "position": {"cabinet_id": "CAB-A", "zone_id": "Z-other",
                            "tray_id": "T-o1"}}
        res = engine.evaluate_layout(self.m, self.fac, [org])
        self.assertIn("VENTILATION_INSUFFICIENT", self.issues_by_code(res))

    def test_clean_layout_disposable(self):
        # 单桶酸，位置/材质/装填/托盘/通风全部满足
        res = engine.evaluate_layout(self.m, self.fac, [acid()])
        self.assertEqual(res["disposition"], "disposable",
                         msg=json.dumps(res["issues"], ensure_ascii=False))
        self.assertEqual(res["summary"]["blockers"], 0)

    def test_unplaced_container_pending(self):
        a = acid()
        a["position"] = None
        res = engine.evaluate_layout(self.m, self.fac, [a])
        self.assertEqual(res["disposition"], "pending")
        self.assertIn("UNPLACED", self.issues_by_code(res))

    def test_result_is_deterministic_fingerprint(self):
        r1 = engine.evaluate_layout(self.m, self.fac, [acid()])
        r2 = engine.evaluate_layout(self.m, self.fac, [acid()])
        self.assertEqual(r1["fingerprint"], r2["fingerprint"])
        moved = acid()
        moved["position"] = {"cabinet_id": "CAB-A", "zone_id": "Z-other",
                             "tray_id": "T-o1"}
        r3 = engine.evaluate_layout(self.m, self.fac, [moved])
        self.assertNotEqual(r1["fingerprint"], r3["fingerprint"])

    def test_merge_weighted_components(self):
        merged = engine.merge_containers(
            {"id": "M1", "material": "hdpe", "capacity_l": 100,
             "position": None},
            [{"id": "x", "volume_l": 40,
              "components": [{"name": "nacn", "conc_min": 10,
                              "conc_max": 10}], "hazard_classes": ["cyanide"]},
             {"id": "y", "volume_l": 60,
              "components": [{"name": "hcl", "conc_min": 20,
                              "conc_max": 20}], "hazard_classes": ["acid"]}])
        comp = {c["name"]: c for c in merged["components"]}
        self.assertAlmostEqual(comp["nacn"]["conc_min"], 4.0)
        self.assertAlmostEqual(comp["hcl"]["conc_max"], 12.0)
        self.assertEqual(merged["volume_l"], 100)

    def _merged_acid_cyanide(self, target_cap=60, tray="T-a2"):
        """模拟 storage 的合并：20L 酸 + 20L 含氰 -> 目标桶，附叶级溯源。"""
        a = acid()
        k = cyanide()
        target = {"id": "M1", "material": "hdpe",
                  "capacity_l": target_cap,
                  "position": {"cabinet_id": "CAB-A", "zone_id": "Z-acid",
                               "tray_id": tray}}
        merged = engine.merge_containers(target, [a, k])
        merged["merged_from"] = ["A1", "K1"]
        merged["merge_provenance"] = [
            {"source_id": "A1", "position": a["position"],
             "volume_l": a["volume_l"], "capacity_l": a["capacity_l"],
             "material": a["material"], "components": a["components"],
             "hazard_classes": a["hazard_classes"]},
            {"source_id": "K1", "position": k["position"],
             "volume_l": k["volume_l"], "capacity_l": k["capacity_l"],
             "material": k["material"], "components": k["components"],
             "hazard_classes": k["hazard_classes"]},
        ]
        return merged

    def test_merge_incompatible_acid_cyanide_flagged(self):
        # 缺陷回归：酸液与含氰液合并后，待处置结果必须给出
        # R1 / HCN / 两个源桶 / 各自位置 / 依据
        merged = self._merged_acid_cyanide()
        res = engine.evaluate_layout(self.m, self.fac, [merged])
        self.assertEqual(res["disposition"], "pending")
        iss = [i for i in res["issues"]
               if i["code"] == "MERGE_INCOMPATIBLE"]
        self.assertEqual(len(iss), 1)
        i = iss[0]
        self.assertEqual(i["basis"]["rule_id"], "R1")
        self.assertEqual(i["basis"]["rule"]["gas"], "HCN")
        self.assertEqual(set(i["containers"]), {"M1", "A1", "K1"})
        # 两个源桶合并前的位置都要返回
        self.assertEqual(i["basis"]["source_positions"]["A1"]["tray_id"],
                         "T-a1")
        self.assertEqual(i["basis"]["source_positions"]["K1"]["tray_id"],
                         "T-c1")
        self.assertEqual(i["positions"]["A1"]["tray_id"], "T-a1")
        self.assertEqual(i["positions"]["K1"]["tray_id"], "T-c1")
        # 计算明细中的 merges 段同样可追溯
        md = res["calculation"]["merges"][0]
        self.assertEqual(md["source_containers"], ["A1", "K1"])
        pair = md["pair_checks"][0]
        self.assertEqual(pair["sources"], ["A1", "K1"])
        self.assertEqual(pair["matched_rules"][0]["rule_id"], "R1")

    def test_merge_volume_fill_and_tray_not_distorted(self):
        # 缺陷回归：合并后装量=源桶装量之和（不重复计量），装填率与
        # 盛漏按目标桶/目标托盘如实计算
        merged = self._merged_acid_cyanide(target_cap=60, tray="T-a2")
        res = engine.evaluate_layout(self.m, self.fac, [merged])
        # 装量 40L，不因溯源而重复累加
        self.assertEqual(merged["volume_l"], 40)
        pc = next(x for x in res["per_container"]
                  if x["container_id"] == "M1")
        self.assertAlmostEqual(pc["fill_ratio"], 40 / 60, places=6)
        # 托盘 T-a2 公称 200L -> 有效 180L；需 40*1.1=44L，充足
        tray = next(t for t in res["calculation"]["trays"]
                    if t["tray_id"] == "T-a2")
        self.assertEqual(tray["containers"], ["M1"])
        self.assertEqual(tray["total_volume_l"], 40)
        self.assertEqual(tray["required_containment_l"], 44.0)
        self.assertEqual(tray["usable_capacity_l"], 180.0)
        self.assertTrue(tray["sufficient"])
        # 除合并禁配外不应出现盛漏/装填类问题
        codes = {i["code"] for i in res["issues"]}
        self.assertNotIn("TRAY_CONTAINMENT_INSUFFICIENT", codes)
        self.assertNotIn("FILL_OVERFLOW", codes)

    def test_merge_chained_provenance_classifies_leaves(self):
        # 链式合并：M1(酸) 再与含氰桶合并，叶级溯源展开后仍须命中 R1
        first = self._merged_acid_cyanide()  # M1 含 A1,K1 溯源但已禁配
        # 构造一个“只含酸”的中间合并桶，再与含氰桶合并
        a = acid()
        mid_target = {"id": "MID", "material": "hdpe", "capacity_l": 60,
                      "position": a["position"]}
        mid = engine.merge_containers(mid_target, [a])
        mid["merged_from"] = ["A1"]
        mid["merge_provenance"] = [{
            "source_id": "A1", "position": a["position"],
            "volume_l": a["volume_l"], "capacity_l": a["capacity_l"],
            "material": a["material"], "components": a["components"],
            "hazard_classes": a["hazard_classes"]}]
        k = cyanide()
        t2 = {"id": "M2", "material": "hdpe", "capacity_l": 60,
              "position": {"cabinet_id": "CAB-A", "zone_id": "Z-acid",
                           "tray_id": "T-a2"}}
        final = engine.merge_containers(t2, [mid, k])
        # 复刻 storage._merge_provenance 的叶展开逻辑
        from hazwaste.storage import Store
        state = {"MID": mid, "K1": k}
        final["merge_provenance"] = Store._merge_provenance(["MID", "K1"],
                                                            state)
        res = engine.evaluate_layout(self.m, self.fac, [final])
        iss = [i for i in res["issues"]
               if i["code"] == "MERGE_INCOMPATIBLE"]
        self.assertTrue(any(i["basis"]["rule_id"] == "R1" for i in iss))
        leaf_ids = {p["source_id"] for p in final["merge_provenance"]}
        self.assertEqual(leaf_ids, {"A1", "K1"})


class MatrixTests(unittest.TestCase):
    def test_validate_catches_duplicate_pair(self):
        m = M.build_v1()
        m["reactions"].append(M._re("R99", "acid", "cyanide", "heat"))
        errs = M.validate_matrix(m)
        self.assertTrue(any("反应规则冲突" in e for e in errs))

    def test_v1_v2_diff(self):
        d = engine.diff_matrices(M.build_v1(), M.build_v2())
        self.assertTrue(d["has_changes"])
        self.assertIn("max_fill_ratio", d["limits_changed"])
        self.assertEqual(d["limits_changed"]["max_fill_ratio"]["new"], 0.85)
        self.assertEqual([r["id"] for r in d["reactions_added"]], ["R8"])
        r3 = next(r for r in d["reactions_changed"] if r["rule_id"] == "R3")
        self.assertEqual(r3["fields"]["min_distance_m"]["new"], 1.5)


class MergeStorageTests(unittest.TestCase):
    """合并事件在存储层的边界。"""

    def setUp(self):
        self.store = Store(":memory:")
        self.store.put_facility("F1", facility())

    def _intake(self, c):
        state = self.store.replay_state("F1")
        state = self.store._apply_event(
            "intake", c["id"], {"container": c}, state)
        return state

    def test_duplicate_source_ids_rejected(self):
        a = acid()
        k = cyanide()
        state = self._intake(a)
        state = self.store._apply_event(
            "intake", k["id"], {"container": k}, state)
        target = {"id": "M1", "material": "hdpe", "capacity_l": 60,
                  "position": {"cabinet_id": "CAB-A", "zone_id": "Z-acid",
                               "tray_id": "T-a2"}}
        with self.assertRaises(StoreError) as ctx:
            self.store._apply_event(
                "merge", None,
                {"source_ids": ["A1", "K1", "A1"], "target": target}, state)
        self.assertEqual(ctx.exception.code, "MERGE_DUPLICATE_SOURCE")
        self.assertEqual(ctx.exception.status, 422)
        self.assertEqual(ctx.exception.detail["duplicates"], ["A1"])
        # 被拒绝的合并不产生任何状态变化：两个源桶都还在
        self.assertIn("A1", state)
        self.assertIn("K1", state)
        self.assertNotIn("M1", state)

    def test_duplicate_source_would_otherwise_double_count(self):
        # 正常合并 [A1,K1] 装量=40；若不去重 [A1,A1] 会把 20L 算成 40/60，
        # 这里直接验证合法路径装量与浓度不失真
        a = acid()
        k = cyanide()
        state = self._intake(a)
        state = self.store._apply_event(
            "intake", k["id"], {"container": k}, state)
        target = {"id": "M1", "material": "hdpe", "capacity_l": 60,
                  "position": {"cabinet_id": "CAB-A", "zone_id": "Z-acid",
                               "tray_id": "T-a2"}}
        state = self.store._apply_event(
            "merge", None,
            {"source_ids": ["A1", "K1"], "target": target}, state)
        self.assertEqual(state["M1"]["volume_l"], 40)
        self.assertEqual(len(state["M1"]["merge_provenance"]), 2)
        self.assertEqual(sorted(p["source_id"]
                                for p in state["M1"]["merge_provenance"]),
                         ["A1", "K1"])


if __name__ == "__main__":
    unittest.main()
