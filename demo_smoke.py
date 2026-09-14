"""端到端冒烟脚本（真实 HTTP 服务）：酸桶 + 含氰桶入同一柜体。

演示：设施注册 -> 两桶入库 -> 待处置（HCN 禁配）-> 移动预检拒绝 ->
移出柜后重算可处置 -> 确认 -> 持久化重启后旧修订仍不可改写。
"""
import json
import sys
import urllib.request
import urllib.error

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8931"
FID = "demo-lab"


def call(method, path, payload=None):
    req = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(payload, ensure_ascii=False).encode()
        if payload is not None else None,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


facility = {
    "id": FID,
    "cabinets": [
        {"id": "CAB-1", "ventilation": "local_exhaust", "zones": [
            {"id": "Z1", "position_xy": [0, 0], "trays": [
                {"id": "T1", "capacity_l": 60}]},
            {"id": "Z2", "position_xy": [0.3, 0], "trays": [
                {"id": "T2", "capacity_l": 60}]}]},
        {"id": "CAB-2", "ventilation": "explosion_proof", "zones": [
            {"id": "Z9", "position_xy": [0, 0], "trays": [
                {"id": "T9", "capacity_l": 120}]}]},
    ],
}
acid = {"id": "DRUM-A",
        "components": [{"name": "hcl", "conc_min": 30, "conc_max": 33}],
        "hazard_classes": ["acid"], "material": "hdpe",
        "capacity_l": 25, "volume_l": 20,
        "position": {"cabinet_id": "CAB-1", "zone_id": "Z1", "tray_id": "T1"}}
cyan = {"id": "DRUM-CN",
        "components": [{"name": "nacn", "conc_min": 4, "conc_max": 9}],
        "hazard_classes": ["cyanide"], "material": "hdpe",
        "capacity_l": 25, "volume_l": 20,
        "position": {"cabinet_id": "CAB-1", "zone_id": "Z2", "tray_id": "T2"}}

st, _ = call("PUT", f"/facilities/{FID}", facility)
print("PUT facility:", st)
st, r = call("POST", f"/facilities/{FID}/events",
             {"matrix_version": "1.0",
              "event": {"ts": "2026-09-14T09:00:00Z", "type": "intake",
                        "container_id": acid["id"],
                        "payload": {"container": acid}}})
print("intake acid:", st, r["latest"])
st, r = call("POST", f"/facilities/{FID}/events",
             {"matrix_version": "1.0",
              "event": {"ts": "2026-09-14T09:05:00Z", "type": "intake",
                        "container_id": cyan["id"],
                        "payload": {"container": cyan}}})
print("intake cyanide:", st, "->", r["latest"])
pending_rev = r["revisions"][-1]["revision_id"]

# 确认必须被拒
st, err = call("POST", f"/revisions/{pending_rev}/confirm", {})
print("confirm pending ->", st, err["error"]["code"])
b = err["error"]["detail"]["blockers"][0]
print("  blocker:", b["code"], b["containers"],
      "rule:", b["basis"]["rule_id"], "gas:", b["basis"]["rule"]["gas"])

# 移动预检：把氰桶留在 CAB-1（已经同柜）必然不行；预检移出到 CAB-2
st, mc = call("POST", f"/facilities/{FID}/layout/move-check",
              {"matrix_version": "1.0", "container_id": "DRUM-CN",
               "position": {"cabinet_id": "CAB-2", "zone_id": "Z9",
                            "tray_id": "T9"}})
print("move-check out ->", st, "allowed:", mc["move_verdict"]["allowed"],
      "| trial:", mc["trial_id"])

# 执行移位
st, mv = call("POST", f"/facilities/{FID}/events",
              {"matrix_version": "1.0",
               "event": {"ts": "2026-09-14T10:00:00Z", "type": "move",
                         "container_id": "DRUM-CN",
                         "payload": {"position": {
                             "cabinet_id": "CAB-2", "zone_id": "Z9",
                             "tray_id": "T9"}}}})
print("move event ->", mv["latest"])
ok_rev = mv["revisions"][-1]["revision_id"]
st, cf = call("POST", f"/revisions/{ok_rev}/confirm", {"note": "复核通过"})
print("confirm ->", st, "confirmations:", len(cf["confirmations"]))

# 版本比较
st, d = call("GET", f"/revisions/{pending_rev}/diff/{ok_rev}")
print("rev diff:", d["disposition"],
      "resolved:", [x["code"] for x in d["issues_resolved"]])

# 逐桶处置单
st, sh = call("GET", f"/revisions/{ok_rev}/disposal?container_id=DRUM-A")
print("disposal sheet:", st, sh["container_disposition"],
      "fill:", sh["fill_check"]["fill_ratio"],
      "tray_required_L:",
      sh["tray_containment_check"]["required_containment_l"])

# 矩阵版本比较
st, md = call("GET", "/matrices/1.0/diff/2.0")
print("matrix diff has changes:", md["has_changes"],
      "| added rules:", [r["id"] for r in md["reactions_added"]],
      "| fill limit:", md["limits_changed"]["max_fill_ratio"])

print("PENDING_REV", pending_rev)
print("OK_REV", ok_rev)
