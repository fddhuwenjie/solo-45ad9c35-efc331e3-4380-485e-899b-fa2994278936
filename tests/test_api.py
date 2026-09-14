"""HTTP API 端到端测试（真实 socket + ThreadingHTTPServer）。"""
import json
import threading
import time
import unittest
import urllib.request
import urllib.error

from hazwaste.server import make_server
from tests.test_engine import facility


def body(obj):
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


class Client:
    def __init__(self, base):
        self.base = base

    def call(self, method, path, payload=None):
        req = urllib.request.Request(
            self.base + path, method=method,
            data=body(payload) if payload is not None else None,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def get(self, p):
        return self.call("GET", p)

    def post(self, p, obj):
        return self.call("POST", p, obj)

    def put(self, p, obj):
        return self.call("PUT", p, obj)


FACILITY = facility()

ACID = {"id": "A1",
        "components": [{"name": "hcl", "conc_min": 30, "conc_max": 32}],
        "hazard_classes": ["acid"], "material": "hdpe",
        "capacity_l": 25, "volume_l": 20,
        "position": {"cabinet_id": "CAB-A", "zone_id": "Z-acid",
                     "tray_id": "T-a1"}}
CYAN = {"id": "K1",
        "components": [{"name": "nacn", "conc_min": 5, "conc_max": 8}],
        "hazard_classes": ["cyanide"], "material": "hdpe",
        "capacity_l": 25, "volume_l": 20,
        "position": {"cabinet_id": "CAB-A", "zone_id": "Z-cyan",
                     "tray_id": "T-c1"}}
ORG = {"id": "G1",
       "components": [{"name": "acetone", "conc_min": 60, "conc_max": 80}],
       "hazard_classes": ["organic"], "material": "hdpe",
       "capacity_l": 25, "volume_l": 20,
       "position": {"cabinet_id": "CAB-B", "zone_id": "Z-org",
                    "tray_id": "T-g1"}}


def intake(container, ts, matrix="1.0", event_id=None):
    return {"matrix_version": matrix, "event": {
        "event_id": event_id, "ts": ts, "type": "intake",
        "container_id": container["id"],
        "payload": {"container": container}}}


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd, cls.store = make_server("127.0.0.1", 0, ":memory:")
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.api = Client(f"http://127.0.0.1:{cls.port}")
        time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.store.close()

    def setUp(self):
        # 每个用例独立设施，避免事件串扰
        self.fid = f"F-{self._testMethodName}"
        f = json.loads(json.dumps(FACILITY))
        f["id"] = self.fid
        st, resp = self.api.put(f"/facilities/{self.fid}", f)
        self.assertEqual(st, 200, resp)

    def _intake(self, c, ts, event_id=None, matrix="1.0"):
        return self.api.post(f"/facilities/{self.fid}/events",
                             intake(c, ts, matrix, event_id))

    def test_01_health_and_matrices(self):
        st, resp = self.api.get("/health")
        self.assertEqual(st, 200)
        self.assertEqual(resp["status"], "ok")
        st, resp = self.api.get("/matrices")
        self.assertEqual(st, 200)
        versions = {m["version"] for m in resp["matrices"]}
        self.assertIn("1.0", versions)
        self.assertIn("2.0", versions)
        st, m1 = self.api.get("/matrices/1.0")
        self.assertEqual(st, 200)
        self.assertTrue(m1["_fingerprint"])

    def test_02_intake_clean_is_disposable_and_confirmable(self):
        st, resp = self._intake(ACID, "2026-09-14T09:00:00Z")
        self.assertEqual(st, 201, resp)
        self.assertEqual(resp["latest"], "disposable")
        rev_id = resp["revisions"][0]["revision_id"]
        st, rev = self.api.get(f"/revisions/{rev_id}")
        self.assertEqual(st, 200)
        self.assertEqual(rev["disposition"], "disposable")
        st, cf = self.api.post(f"/revisions/{rev_id}/confirm",
                               {"note": "核对无误"})
        self.assertEqual(st, 201, cf)
        self.assertEqual(len(cf["confirmations"]), 1)

    def test_03_forbidden_pair_blocks_confirm(self):
        self._intake(ACID, "2026-09-14T09:00:00Z")
        st, resp = self._intake(CYAN, "2026-09-14T09:05:00Z")
        self.assertEqual(resp["latest"], "pending")
        rev_id = resp["revisions"][-1]["revision_id"]
        st, err = self.api.post(f"/revisions/{rev_id}/confirm", {})
        self.assertEqual(st, 409)
        self.assertEqual(err["error"]["code"], "CONFIRM_BLOCKED")
        codes = {b["code"] for b in err["error"]["detail"]["blockers"]}
        self.assertIn("INCOMPATIBLE_COLOCATION", codes)
        # 问题必须带涉事容器、位置、依据
        block = next(b for b in err["error"]["detail"]["blockers"]
                     if b["code"] == "INCOMPATIBLE_COLOCATION")
        self.assertEqual(block["containers"], ["A1", "K1"])
        self.assertIn("T-a1", json.dumps(block))

    def test_04_move_recomputes_layout_and_precheck(self):
        # 酸先进 A 区，氰桶进 CAB-B（安全），再预检把氰桶移进 CAB-A -> 拒绝
        self._intake(ACID, "2026-09-14T09:00:00Z")
        cyan_b = json.loads(json.dumps(CYAN))
        cyan_b["position"] = {"cabinet_id": "CAB-B", "zone_id": "Z-org",
                              "tray_id": "T-g1"}
        st, resp = self._intake(cyan_b, "2026-09-14T09:05:00Z")
        self.assertEqual(resp["latest"], "disposable", resp)

        st, mc = self.api.post(
            f"/facilities/{self.fid}/layout/move-check",
            {"matrix_version": "1.0", "container_id": "K1",
             "position": {"cabinet_id": "CAB-A", "zone_id": "Z-cyan",
                          "tray_id": "T-c1"}})
        self.assertEqual(st, 201)
        self.assertFalse(mc["move_verdict"]["allowed"])
        self.assertEqual(mc["disposition"], "pending")
        introduced = mc["diff_vs_base"]["issues_introduced"]
        self.assertTrue(any(i["code"] == "INCOMPATIBLE_COLOCATION"
                            for i in introduced))
        # 预检不落事件：本设施事件仍只有两条
        st, ev = self.api.get(f"/events?facility_id={self.fid}")
        self.assertEqual(len(ev["events"]), 2)

        # 真正执行移位事件 -> 新修订 pending
        st, moved = self.api.post(
            f"/facilities/{self.fid}/events",
            {"matrix_version": "1.0", "event": {
                "ts": "2026-09-14T10:00:00Z", "type": "move",
                "container_id": "K1",
                "payload": {"position": {
                    "cabinet_id": "CAB-A", "zone_id": "Z-cyan",
                    "tray_id": "T-c1"}}}})
        self.assertEqual(moved["latest"], "pending")
        st, ev = self.api.get(f"/events?facility_id={self.fid}")
        self.assertEqual(len(ev["events"]), 3)

    def test_05_correction_creates_new_revision_old_immutable(self):
        self._intake(ACID, "2026-09-14T09:00:00Z")
        st, latest1 = self.api.get("/revisions/latest")
        rev1 = latest1["revision_id"]
        fp1 = latest1["result"]["fingerprint"]

        # 成分更正：把盐酸改成未知浓度跨阈值的 H2O2（举例更正场景）
        st, resp = self.api.post(
            f"/facilities/{self.fid}/events",
            {"matrix_version": "1.0", "event": {
                "ts": "2026-09-14T11:00:00Z", "type": "correct",
                "container_id": "A1",
                "payload": {
                    "components": [{"name": "h2o2", "conc_min": 5,
                                    "conc_max": 30}],
                    "hazard_classes": ["oxidizer"]}}})
        self.assertEqual(st, 201, resp)
        rev2 = resp["revisions"][0]["revision_id"]
        self.assertNotEqual(rev1, rev2)

        # 旧修订不可改写：指纹、结果保持原样
        st, old = self.api.get(f"/revisions/{rev1}")
        self.assertEqual(old["result"]["fingerprint"], fp1)
        self.assertEqual(old["state"]["A1"]["components"][0]["name"], "hcl")

        # 版本比较指出变化
        st, d = self.api.get(f"/revisions/{rev1}/diff/{rev2}")
        self.assertEqual(d["disposition"], {"old": "disposable",
                                            "new": "pending",
                                            "changed": True})
        chg = next(c for c in d["container_changes"]
                   if c["container_id"] == "A1")
        self.assertIn("hazard_possible", chg["fields"])

    def test_06_trial_does_not_mutate_state(self):
        self._intake(ACID, "2026-09-14T09:00:00Z")
        self._intake(CYAN, "2026-09-14T09:05:00Z")
        # 试排：把 K1 移到 CAB-B，应当消解禁配
        st, tr = self.api.post(
            f"/facilities/{self.fid}/layout/try",
            {"matrix_version": "1.0", "proposal": {"moves": [
                {"container_id": "K1",
                 "position": {"cabinet_id": "CAB-B", "zone_id": "Z-org",
                              "tray_id": "T-g1"}}]}})
        self.assertEqual(st, 201, tr)
        self.assertEqual(tr["disposition"], "disposable")
        self.assertTrue(tr["frozen"])
        self.assertTrue(tr["diff_vs_base"]["issues_resolved"])
        # 试排后真实最新修订仍是 pending
        st, latest = self.api.get("/revisions/latest")
        self.assertEqual(latest["disposition"], "pending")
        # 试排记录可按 id 取回
        st, got = self.api.get(f"/trials/{tr['trial_id']}")
        self.assertEqual(st, 200)
        self.assertEqual(got["result"]["fingerprint"],
                         tr["result"]["fingerprint"])

    def test_07_ambiguous_range_pending(self):
        maybe = {"id": "X1",
                 "components": [{"name": "nacn", "conc_min": 0.5,
                                 "conc_max": 2.0}],
                 "hazard_classes": [], "material": "hdpe",
                 "capacity_l": 25, "volume_l": 20,
                 "position": {"cabinet_id": "CAB-B", "zone_id": "Z-org",
                              "tray_id": "T-g1"}}
        st, resp = self._intake(maybe, "2026-09-14T09:00:00Z")
        self.assertEqual(resp["latest"], "pending")
        st, rev = self.api.get("/revisions/latest")
        codes = {i["code"] for i in rev["result"]["issues"]}
        self.assertIn("CLASS_AMBIGUOUS", codes)

    def test_08_recalc_verifies_and_disposal_sheet(self):
        self._intake(ACID, "2026-09-14T09:00:00Z")
        st, rev = self.api.get("/revisions/latest")
        rid = rev["revision_id"]

        st, rc = self.api.post(f"/revisions/{rid}/recalc", {})
        self.assertEqual(st, 200, rc)
        self.assertTrue(rc["verified"])
        self.assertEqual(rc["recalculated_fingerprint"],
                         rc["stored_fingerprint"])
        self.assertEqual(rc["limits"]["max_fill_ratio"], 0.90)

        st, sh = self.api.get(
            f"/revisions/{rid}/disposal?container_id=A1")
        self.assertEqual(st, 200, sh)
        self.assertEqual(sh["container_disposition"], "disposable")
        self.assertEqual(sh["classification_detail"]["definite"],
                         ["acid", "corrosive"])
        self.assertAlmostEqual(sh["fill_check"]["fill_ratio"], 0.8)
        self.assertTrue(sh["tray_containment_check"]["sufficient"])
        self.assertEqual(sh["tray_containment_check"]
                         ["required_containment_l"], 22.0)
        self.assertTrue(sh["result_fingerprint"])

    def test_09_merge_event_and_disposal(self):
        self._intake(ACID, "2026-09-14T09:00:00Z", event_id="e-acid")
        ox = {"id": "OX1",
              "components": [{"name": "h2o2", "conc_min": 30,
                              "conc_max": 35}],
              "hazard_classes": ["oxidizer"], "material": "hdpe",
              "capacity_l": 25, "volume_l": 20,
              "position": {"cabinet_id": "CAB-B", "zone_id": "Z-org",
                           "tray_id": "T-g1"}}
        self._intake(ox, "2026-09-14T09:05:00Z", event_id="e-ox")
        # 把酸并入有机柜里的氧化剂桶（混合后出现 acid+oxidizer 类，且和 G1 有机同柜）
        st, resp = self.api.post(
            f"/facilities/{self.fid}/events",
            {"matrix_version": "1.0", "event": {
                "ts": "2026-09-14T12:00:00Z", "type": "merge",
                "payload": {"source_ids": ["A1", "OX1"],
                            "target": {"id": "M1", "material": "hdpe",
                                       "capacity_l": 60,
                                       "position": {
                                           "cabinet_id": "CAB-B",
                                           "zone_id": "Z-org",
                                           "tray_id": "T-g1"}}}}})
        self.assertEqual(st, 201, resp)
        st, rev = self.api.get(
            f"/revisions/latest?facility_id={self.fid}")
        self.assertIn("M1", rev["state"])
        self.assertNotIn("A1", rev["state"])
        comps = {c["name"] for c in rev["state"]["M1"]["components"]}
        self.assertEqual(comps, {"hcl", "h2o2"})

    def test_09b_merge_acid_cyanide_returns_r1_with_sources(self):
        # 缺陷回归：酸液与含氰液合并，待处置结果须含 R1/HCN/两个源桶/
        # 各自位置/依据；且合并装量、装填率、盛漏计算不失真
        self._intake(ACID, "2026-09-14T09:00:00Z", event_id="m-acid")
        self._intake(CYAN, "2026-09-14T09:05:00Z", event_id="m-cyan")
        st, resp = self.api.post(
            f"/facilities/{self.fid}/events",
            {"matrix_version": "1.0", "event": {
                "ts": "2026-09-14T12:00:00Z", "type": "merge",
                "payload": {"source_ids": ["A1", "K1"],
                            "target": {"id": "M1", "material": "hdpe",
                                       "capacity_l": 60,
                                       "position": {
                                           "cabinet_id": "CAB-A",
                                           "zone_id": "Z-acid",
                                           "tray_id": "T-a2"}}}}})
        self.assertEqual(st, 201, resp)
        self.assertEqual(resp["latest"], "pending")
        rev_id = resp["revisions"][-1]["revision_id"]
        st, rev = self.api.get(f"/revisions/{rev_id}")

        issue = next(i for i in rev["result"]["issues"]
                     if i["code"] == "MERGE_INCOMPATIBLE")
        self.assertEqual(issue["basis"]["rule_id"], "R1")
        self.assertEqual(issue["basis"]["rule"]["gas"], "HCN")
        self.assertEqual(set(issue["containers"]), {"M1", "A1", "K1"})
        self.assertEqual(issue["basis"]["source_positions"]["A1"],
                         ACID["position"])
        self.assertEqual(issue["basis"]["source_positions"]["K1"],
                         CYAN["position"])
        self.assertEqual(issue["positions"]["A1"]["tray_id"], "T-a1")
        self.assertEqual(issue["positions"]["K1"]["tray_id"], "T-c1")

        # 合并后装量 40L（不重复计量）、装填率 40/60
        self.assertEqual(rev["state"]["M1"]["volume_l"], 40)
        pc = next(x for x in rev["result"]["per_container"]
                  if x["container_id"] == "M1")
        self.assertAlmostEqual(pc["fill_ratio"], 40 / 60, places=6)
        # 盛漏：T-a2 公称 200L 有效 180L；需 40*1.1=44L，充足
        tray = next(t for t in rev["result"]["calculation"]["trays"]
                    if t["tray_id"] == "T-a2")
        self.assertEqual(tray["containers"], ["M1"])
        self.assertEqual(tray["total_volume_l"], 40)
        self.assertEqual(tray["required_containment_l"], 44.0)
        self.assertTrue(tray["sufficient"])
        codes = {i["code"] for i in rev["result"]["issues"]}
        self.assertNotIn("TRAY_CONTAINMENT_INSUFFICIENT", codes)
        self.assertNotIn("FILL_OVERFLOW", codes)

        # 待处置不可确认
        st, err = self.api.post(f"/revisions/{rev_id}/confirm", {})
        self.assertEqual(st, 409)
        self.assertEqual(err["error"]["code"], "CONFIRM_BLOCKED")

        # 逐桶处置单含合并自检段与源桶依据
        st, sh = self.api.get(
            f"/revisions/{rev_id}/disposal?container_id=M1")
        self.assertEqual(st, 200)
        self.assertEqual(sh["container_disposition"], "pending")
        self.assertAlmostEqual(sh["fill_check"]["fill_ratio"], 40 / 60,
                               places=6)
        mc = sh["merge_self_check"]
        self.assertEqual(mc["source_containers"], ["A1", "K1"])
        self.assertEqual(mc["pair_checks"][0]["matched_rules"][0]["rule_id"],
                         "R1")
        self.assertTrue(any("R1" in a for a in sh["required_actions"]))

        # 复算 JSON：冻结矩阵重算，指纹一致，计算明细保留
        st, rc = self.api.post(f"/revisions/{rev_id}/recalc", {})
        self.assertEqual(st, 200)
        self.assertTrue(rc["verified"])
        self.assertEqual(rc["recalculated_fingerprint"],
                         rc["stored_fingerprint"])
        merge_calc = [m for m in rc["result"]["calculation"]["merges"]
                      if m["merged_container"] == "M1"]
        self.assertEqual(
            merge_calc[0]["pair_checks"][0]["matched_rules"][0]["rule_id"],
            "R1")

    def test_09c_merge_duplicate_source_ids_rejected(self):
        # 缺陷回归：重复 source_ids 返回 422，状态不变，不产生修订
        self._intake(ACID, "2026-09-14T09:00:00Z", event_id="d-acid")
        self._intake(CYAN, "2026-09-14T09:05:00Z", event_id="d-cyan")
        revs_before = self.api.get(
            f"/revisions?facility_id={self.fid}")[1]["revisions"]
        st, err = self.api.post(
            f"/facilities/{self.fid}/events",
            {"matrix_version": "1.0", "event": {
                "ts": "2026-09-14T12:00:00Z", "type": "merge",
                "payload": {"source_ids": ["A1", "K1", "A1"],
                            "target": {"id": "M1", "material": "hdpe",
                                       "capacity_l": 60}}}})
        self.assertEqual(st, 422)
        self.assertEqual(err["error"]["code"], "MERGE_DUPLICATE_SOURCE")
        self.assertEqual(err["error"]["detail"]["duplicates"], ["A1"])
        revs_after = self.api.get(
            f"/revisions?facility_id={self.fid}")[1]["revisions"]
        self.assertEqual(len(revs_after), len(revs_before))
        st, ev = self.api.get(f"/events?facility_id={self.fid}")
        self.assertEqual(
            [e["type"] for e in ev["events"]], ["intake", "intake"])

    def test_09d_point_concentration_intake_classified(self):
        # 缺陷回归：HCl [30,30] 点浓度入库被正确判定为酸，布局可处置
        point_acid = json.loads(json.dumps(ACID))
        point_acid["components"] = [
            {"name": "hcl", "conc_min": 30, "conc_max": 30}]
        st, resp = self._intake(point_acid, "2026-09-14T09:00:00Z")
        self.assertEqual(resp["latest"], "disposable", resp)
        rid = resp["revisions"][-1]["revision_id"]
        st, rev = self.api.get(f"/revisions/{rid}")
        cls = rev["result"]["calculation"]["classification"]["A1"]
        self.assertEqual(cls["definite"], ["acid", "corrosive"])
        self.assertFalse(cls["ambiguous"])

    def test_10_repack_event_new_container(self):
        self._intake(ACID, "2026-09-14T09:00:00Z")
        st, resp = self.api.post(
            f"/facilities/{self.fid}/events",
            {"matrix_version": "1.0", "event": {
                "ts": "2026-09-14T13:00:00Z", "type": "repack",
                "container_id": "A1",
                "payload": {"container": {
                    "id": "A2", "material": "pp", "capacity_l": 30,
                    "volume_l": 18,
                    "position": {"cabinet_id": "CAB-A", "zone_id": "Z-acid",
                                 "tray_id": "T-a2"}}}}})
        self.assertEqual(st, 201, resp)
        st, rev = self.api.get("/revisions/latest")
        self.assertIn("A2", rev["state"])
        self.assertNotIn("A1", rev["state"])
        self.assertEqual(rev["state"]["A2"]["replaced_from"], "A1")

    def test_11_matrix_freeze_conflict_and_diff(self):
        # 用同版本号冻结不同内容 -> 409，旧矩阵不可改写
        m = self.api.get("/matrices/1.0")[1]
        m["limits"]["max_fill_ratio"] = 0.5
        st, err = self.api.post("/matrices", m)
        self.assertEqual(st, 409)
        self.assertEqual(err["error"]["code"], "MATRIX_VERSION_CONFLICT")

        st, d = self.api.get("/matrices/1.0/diff/2.0")
        self.assertEqual(st, 200)
        self.assertTrue(d["has_changes"])
        self.assertEqual([r["id"] for r in d["reactions_added"]], ["R8"])

    def test_12_frozen_matrix_shared_across_endpoints(self):
        # 用 2.0 矩阵入库：2.0 下装填上限 0.85，20/25=0.8 -> 仍通过
        st, resp = self._intake(ACID, "2026-09-14T09:00:00Z", matrix="2.0")
        self.assertEqual(resp["latest"], "disposable", resp)
        rid = resp["revisions"][0]["revision_id"]
        # 试排也必须带同一冻结版本；无影响的原地试排，指纹与修订一致
        st, tr = self.api.post(
            f"/facilities/{self.fid}/layout/try",
            {"matrix_version": "2.0", "proposal": {"moves": [
                {"container_id": "A1",
                 "position": {"cabinet_id": "CAB-A", "zone_id": "Z-acid",
                              "tray_id": "T-a1"}}]}})
        self.assertEqual(st, 201)
        self.assertEqual(tr["matrix_fingerprint"],
                         resp["revisions"][0]["matrix_fingerprint"])
        st, rev = self.api.get(f"/revisions/{rid}")
        self.assertEqual(tr["result"]["fingerprint"],
                         rev["result"]["fingerprint"])
        # 确认时校验试排与修订矩阵一致
        st, cf = self.api.post(f"/revisions/{rid}/confirm",
                               {"trial_id": tr["trial_id"]})
        self.assertEqual(st, 201, cf)

    def test_13_unknown_matrix_rejected(self):
        st, err = self._intake(ACID, "2026-09-14T09:00:00Z", matrix="9.9")
        self.assertEqual(st, 404)
        self.assertEqual(err["error"]["code"], "MATRIX_NOT_FOUND")

    def test_14_event_ts_ordering_and_dup(self):
        self._intake(ACID, "2026-09-14T09:00:00Z", event_id="e1")
        st, err = self._intake(ORG, "2026-09-14T08:00:00Z")
        self.assertEqual(st, 409)
        self.assertEqual(err["error"]["code"], "EVENT_TS_OUT_OF_ORDER")
        st, err = self._intake(ACID, "2026-09-14T10:00:00Z", event_id="e1")
        self.assertEqual(st, 409)
        self.assertEqual(err["error"]["code"], "EVENT_DUP")
    def test_15_batch_events_multiple_revisions(self):
        payload = {"matrix_version": "1.0", "events": [
            {"ts": "2026-09-14T09:00:00Z", "type": "intake",
             "container_id": "A1", "payload": {"container": ACID}},
            {"ts": "2026-09-14T09:05:00Z", "type": "move",
             "container_id": "A1",
             "payload": {"position": {"cabinet_id": "CAB-A",
                                      "zone_id": "Z-other",
                                      "tray_id": "T-o1"}}}]}
        st, resp = self.api.post(f"/facilities/{self.fid}/events", payload)
        self.assertEqual(st, 201)
        self.assertEqual(resp["applied"], 2)
        self.assertEqual(len(resp["revisions"]), 2)
        self.assertEqual(resp["revisions"][1]["kind"], "move")

    def test_16_label_same_but_incompatible(self):
        # 两瓶标签都写“废酸”，但一瓶实际含次氯酸盐 -> R6 放 Cl2
        acid = json.loads(json.dumps(ACID))
        acid["id"] = "S1"
        bleach = {"id": "S2",
                  "components": [{"name": "hypochlorite",
                                  "conc_min": 10, "conc_max": 12}],
                  "hazard_classes": ["halogen"], "material": "hdpe",
                  "capacity_l": 25, "volume_l": 20,
                  "position": {"cabinet_id": "CAB-A", "zone_id": "Z-cyan",
                               "tray_id": "T-c1"}}
        self._intake(acid, "2026-09-14T09:00:00Z")
        st, resp = self._intake(bleach, "2026-09-14T09:05:00Z")
        self.assertEqual(resp["latest"], "pending")
        st, rev = self.api.get("/revisions/latest")
        rules = {i["basis"]["rule_id"] for i in rev["result"]["issues"]
                 if i["code"] == "INCOMPATIBLE_COLOCATION"}
        self.assertIn("R6", rules)


if __name__ == "__main__":
    unittest.main()
