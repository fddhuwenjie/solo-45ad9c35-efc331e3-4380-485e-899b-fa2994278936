"""sqlite3 仅追加存储层。

不可改写原则：
- matrices：冻结后只可 INSERT；
- events：事件日志只可 INSERT，按 (ts, seq) 顺序回放；
- revisions：每次入库/移位/合并/更正/换桶都产生新修订行，旧行永不更新；
- trials：试排/移动预检的计算结果同样落库冻结；
- confirmations：确认不回写修订，而是新增确认记录引用修订；
- facility_versions：设施（柜体/分区/托盘/通风）定义也按版本追加。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import engine, matrix as M

SCHEMA = """
CREATE TABLE IF NOT EXISTS matrices (
    version      TEXT PRIMARY KEY,
    fingerprint  TEXT NOT NULL,
    body         TEXT NOT NULL,
    source       TEXT NOT NULL,
    frozen_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS facility_versions (
    id           TEXT PRIMARY KEY,
    facility_id  TEXT NOT NULL,
    version      INTEGER NOT NULL,
    body         TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    UNIQUE(facility_id, version)
);
CREATE TABLE IF NOT EXISTS events (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id     TEXT NOT NULL UNIQUE,
    ts           TEXT NOT NULL,
    type         TEXT NOT NULL,
    container_id TEXT,
    payload      TEXT NOT NULL,
    facility_id  TEXT NOT NULL,
    matrix_version TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS revisions (
    rev_id             TEXT PRIMARY KEY,
    parent_rev         TEXT,
    facility_id        TEXT NOT NULL,
    facility_version   INTEGER NOT NULL,
    matrix_version     TEXT NOT NULL,
    matrix_fingerprint TEXT NOT NULL,
    kind               TEXT NOT NULL,
    trigger_event_seq  INTEGER,
    trigger_event_id   TEXT,
    state              TEXT NOT NULL,
    result             TEXT NOT NULL,
    disposition        TEXT NOT NULL,
    created_ts         TEXT NOT NULL,
    created_seq        INTEGER
);
CREATE TABLE IF NOT EXISTS confirmations (
    id            TEXT PRIMARY KEY,
    rev_id        TEXT NOT NULL REFERENCES revisions(rev_id),
    trial_id      TEXT,
    matrix_version TEXT NOT NULL,
    confirmed_at  TEXT NOT NULL,
    note          TEXT
);
CREATE TABLE IF NOT EXISTS trials (
    trial_id           TEXT PRIMARY KEY,
    kind               TEXT NOT NULL,
    base_rev           TEXT,
    facility_id        TEXT NOT NULL,
    matrix_version     TEXT NOT NULL,
    matrix_fingerprint TEXT NOT NULL,
    proposal           TEXT NOT NULL,
    state              TEXT NOT NULL,
    result             TEXT NOT NULL,
    disposition        TEXT NOT NULL,
    created_at         TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class StoreError(Exception):
    def __init__(self, code: str, message: str, status: int = 400,
                 detail: Any = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.detail = detail


class Store:
    def __init__(self, path: str = ":memory:"):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.lock = threading.RLock()
        with self.conn:
            self.conn.executescript(SCHEMA)
        self._seed_builtin_matrices()

    def close(self):
        self.conn.close()

    # ------------------------------------------------------------------ #
    # 矩阵
    # ------------------------------------------------------------------ #
    def _seed_builtin_matrices(self):
        for ver, builder in M.BUILTIN.items():
            row = self.conn.execute(
                "SELECT 1 FROM matrices WHERE version=?", (ver,)).fetchone()
            if row:
                continue
            self.freeze_matrix(builder(), source="builtin")

    def freeze_matrix(self, body: Dict[str, Any],
                      source: str = "uploaded") -> Dict[str, Any]:
        """校验并冻结一个矩阵版本。已存在的版本内容必须完全一致。"""
        problems = M.validate_matrix(body)
        if problems:
            raise StoreError("MATRIX_INVALID",
                             f"矩阵校验失败: {'; '.join(problems)}", 422,
                             problems)
        version = body.get("version")
        if not version:
            raise StoreError("MATRIX_NO_VERSION", "矩阵缺少 version 字段")
        fp = M.matrix_fingerprint(body)
        stored = body.copy()
        stored["_fingerprint"] = fp
        with self.lock, self.conn:
            row = self.conn.execute(
                "SELECT body, fingerprint FROM matrices WHERE version=?",
                (version,)).fetchone()
            if row is not None:
                if row["fingerprint"] != fp:
                    raise StoreError(
                        "MATRIX_VERSION_CONFLICT",
                        f"矩阵版本 {version} 已冻结且内容不同，禁止改写；"
                        "请使用新版本号", 409,
                        {"stored_fingerprint": row["fingerprint"],
                         "new_fingerprint": fp})
                return self.load_matrix(version)
            self.conn.execute(
                "INSERT INTO matrices(version, fingerprint, body, source,"
                " frozen_at) VALUES(?,?,?,?,?)",
                (version, fp, json.dumps(stored, ensure_ascii=False),
                 source, now_iso()))
        return self.load_matrix(version)

    def load_matrix(self, version: str) -> Dict[str, Any]:
        row = self.conn.execute(
            "SELECT body, fingerprint FROM matrices WHERE version=?",
            (version,)).fetchone()
        if row is None:
            raise StoreError("MATRIX_NOT_FOUND",
                             f"冻结矩阵版本 {version} 不存在", 404)
        body = json.loads(row["body"])
        body["_fingerprint"] = row["fingerprint"]
        return body

    def latest_matrix_version(self) -> str:
        row = self.conn.execute(
            "SELECT version FROM matrices ORDER BY frozen_at DESC, rowid DESC"
            " LIMIT 1").fetchone()
        if row is None:
            raise StoreError("NO_MATRIX", "库中没有任何冻结矩阵", 500)
        return row["version"]

    def list_matrices(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT version, fingerprint, source, frozen_at FROM matrices"
            " ORDER BY frozen_at").fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # 设施
    # ------------------------------------------------------------------ #
    def put_facility(self, facility_id: str,
                     body: Dict[str, Any]) -> Dict[str, Any]:
        self._validate_facility(body)
        with self.lock, self.conn:
            row = self.conn.execute(
                "SELECT COALESCE(MAX(version),0) AS v FROM facility_versions"
                " WHERE facility_id=?", (facility_id,)).fetchone()
            ver = row["v"] + 1
            fid = new_id("fac")
            self.conn.execute(
                "INSERT INTO facility_versions(id, facility_id, version,"
                " body, created_at) VALUES(?,?,?,?,?)",
                (fid, facility_id, ver,
                 json.dumps(body, ensure_ascii=False), now_iso()))
        return self.get_facility(facility_id)

    @staticmethod
    def _validate_facility(body: Dict[str, Any]):
        if not body.get("id"):
            raise StoreError("FACILITY_NO_ID", "设施缺少 id")
        cids, zids, tids = set(), set(), set()
        for cab in body.get("cabinets", []):
            if cab["id"] in cids:
                raise StoreError("FACILITY_DUP", f"柜体 id 重复: {cab['id']}")
            cids.add(cab["id"])
            for z in cab.get("zones", []):
                if z["id"] in zids:
                    raise StoreError("FACILITY_DUP",
                                     f"分区 id 重复: {z['id']}")
                zids.add(z["id"])
                for t in z.get("trays", []):
                    if t["id"] in tids:
                        raise StoreError("FACILITY_DUP",
                                         f"托盘 id 重复: {t['id']}")
                    tids.add(t["id"])

    def get_facility(self, facility_id: str,
                     version: Optional[int] = None) -> Dict[str, Any]:
        if version is None:
            row = self.conn.execute(
                "SELECT body, version FROM facility_versions WHERE"
                " facility_id=? ORDER BY version DESC LIMIT 1",
                (facility_id,)).fetchone()
        else:
            row = self.conn.execute(
                "SELECT body, version FROM facility_versions WHERE"
                " facility_id=? AND version=?",
                (facility_id, version)).fetchone()
        if row is None:
            raise StoreError("FACILITY_NOT_FOUND",
                             f"设施 {facility_id} 不存在", 404)
        body = json.loads(row["body"])
        body["_version"] = row["version"]
        return body

    # ------------------------------------------------------------------ #
    # 事件日志
    # ------------------------------------------------------------------ #
    EVENT_TYPES = {"intake", "move", "merge", "correct", "repack"}

    def append_event(self, event: Dict[str, Any], facility_id: str,
                     matrix_version: str,
                     expected_after_ts: Optional[str] = None
                     ) -> Dict[str, Any]:
        """校验并追加单个事件，随后全量重放出新修订（每次移动后重算布局）。

        整个“校验时标 -> 重放 -> 应用 -> 入库 -> 出修订”在同一把锁内完成。
        """
        etype = event.get("type")
        if etype not in self.EVENT_TYPES:
            raise StoreError("EVENT_TYPE_UNKNOWN",
                             f"未知事件类型 {etype}（合法: "
                             f"{sorted(self.EVENT_TYPES)}）", 422)
        ts = event.get("ts")
        if not ts:
            raise StoreError("EVENT_NO_TS", "事件缺少带时标的 ts 字段")
        self._validate_ts(ts)
        payload = event.get("payload") or {}
        cid = event.get("container_id") or payload.get("container_id")
        event_id = event.get("event_id") or new_id("evt")

        with self.lock:
            last = self.conn.execute(
                "SELECT ts, event_id FROM events WHERE facility_id=?"
                " ORDER BY seq DESC LIMIT 1", (facility_id,)).fetchone()
            dup = self.conn.execute(
                "SELECT 1 FROM events WHERE event_id=?",
                (event_id,)).fetchone()
            if dup:
                raise StoreError("EVENT_DUP",
                                 f"事件 {event_id} 已存在，禁止重复追加", 409)
            floor = expected_after_ts or (last["ts"] if last else None)
            if floor is not None and ts < floor:
                raise StoreError(
                    "EVENT_TS_OUT_OF_ORDER",
                    f"事件时标 {ts} 早于本设施已有最新事件 {floor}", 409)

            state = self.replay_state(facility_id)
            try:
                new_state = self._apply_event(etype, cid, payload, state)
            except StoreError:
                raise
            except Exception as exc:  # 防御性：任何应用异常转为 4xx
                raise StoreError("EVENT_INVALID",
                                 f"事件无法应用: {exc}", 422, str(exc))

            with self.conn:
                cur = self.conn.execute(
                    "INSERT INTO events(event_id, ts, type, container_id,"
                    " payload, facility_id, matrix_version)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (event_id, ts, etype, cid,
                     json.dumps(payload, ensure_ascii=False),
                     facility_id, matrix_version))
                seq = cur.lastrowid

            facility = self.get_facility(facility_id)
            matrix = self.load_matrix(matrix_version)
            return self._create_revision(
                new_state, facility, matrix, kind=etype,
                trigger_seq=seq, trigger_event_id=event_id, created_ts=ts)

    @staticmethod
    def _validate_ts(ts: str):
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                datetime.strptime(ts, fmt)
                return
            except ValueError:
                continue
        # 允许带偏移的 ISO 串
        try:
            datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return
        except ValueError:
            raise StoreError("EVENT_BAD_TS",
                             f"时标格式无法解析: {ts}", 422)

    def _apply_event(self, etype: str, cid: Optional[str],
                     payload: Dict[str, Any],
                     state: Dict[str, Dict[str, Any]]
                     ) -> Dict[str, Dict[str, Any]]:
        import copy
        new_state = copy.deepcopy(state)

        if etype == "intake":
            container = payload.get("container")
            self._require_container_fields(container)
            if container["id"] in new_state:
                raise StoreError(
                    "CONTAINER_EXISTS",
                    f"容器 {container['id']} 已入库；成分更正请用 correct，"
                    "换桶请用 repack", 409)
            new_state[container["id"]] = copy.deepcopy(container)

        elif etype == "move":
            if not cid or cid not in new_state:
                raise StoreError("CONTAINER_NOT_FOUND",
                                 f"容器 {cid} 不存在，无法移位", 404)
            if "position" not in payload:
                raise StoreError("MOVE_NO_POSITION",
                                 "move 事件 payload 需要 position", 422)
            new_state[cid]["position"] = copy.deepcopy(payload["position"])

        elif etype == "correct":
            if not cid or cid not in new_state:
                raise StoreError("CONTAINER_NOT_FOUND",
                                 f"容器 {cid} 不存在，无法更正", 404)
            cur = new_state[cid]
            if "components" in payload:
                if not isinstance(payload["components"], list):
                    raise StoreError("CORRECT_INVALID",
                                     "components 必须是列表", 422)
                cur["components"] = copy.deepcopy(payload["components"])
            if "hazard_classes" in payload:
                cur["hazard_classes"] = list(payload["hazard_classes"])
            if "material" in payload:
                cur["material"] = payload["material"]
            cur["_corrections"] = cur.get("_corrections", 0) + 1

        elif etype == "repack":
            if not cid or cid not in new_state:
                raise StoreError("CONTAINER_NOT_FOUND",
                                 f"容器 {cid} 不存在，无法换桶", 404)
            new_container = payload.get("container")
            if not isinstance(new_container, dict) or not new_container.get("id"):
                raise StoreError("CONTAINER_INVALID",
                                 "repack 的 payload.container 需含新桶 id", 422)
            old = new_state.pop(cid)
            nid = new_container["id"]
            if nid in new_state:
                raise StoreError("CONTAINER_EXISTS",
                                 f"新桶 {nid} 已存在", 409)
            # 换桶：旧桶留存于事件历史，当前状态中以新桶替换。
            # components / 装量可省略（继承旧废液），给出时必须合法。
            if "components" in new_container:
                if not isinstance(new_container["components"], list) \
                        or not new_container["components"]:
                    raise StoreError("CONTAINER_INVALID",
                                     "components 必须是非空列表", 422)
            if "capacity_l" in new_container:
                try:
                    if float(new_container["capacity_l"]) <= 0:
                        raise ValueError
                except (TypeError, ValueError):
                    raise StoreError(
                        "CONTAINER_INVALID",
                        f"新桶 {nid} capacity_l 非法", 422)
            if "volume_l" in new_container:
                try:
                    if float(new_container["volume_l"]) < 0:
                        raise ValueError
                except (TypeError, ValueError):
                    raise StoreError(
                        "CONTAINER_INVALID",
                        f"新桶 {nid} volume_l 非法", 422)
            merged = copy.deepcopy(old)
            merged.update({
                "id": nid,
                "components": new_container.get("components", old["components"]),
                "hazard_classes": new_container.get(
                    "hazard_classes", old.get("hazard_classes", [])),
                "material": new_container.get("material", old.get("material")),
                "capacity_l": new_container.get(
                    "capacity_l", old.get("capacity_l")),
                "volume_l": new_container.get("volume_l", old.get("volume_l")),
                "position": new_container.get("position", old.get("position")),
                "replaced_from": cid,
            })
            new_state[nid] = merged

        elif etype == "merge":
            sources = payload.get("source_ids") or []
            target = payload.get("target")
            if not sources or not target:
                raise StoreError(
                    "MERGE_INVALID",
                    "merge 需要 source_ids 与 target", 422)
            missing = [s for s in sources if s not in new_state]
            if missing:
                raise StoreError("CONTAINER_NOT_FOUND",
                                 f"合并源桶不存在: {missing}", 404)
            src_objs = [new_state[s] for s in sources]
            tid = target.get("id")
            if not tid:
                raise StoreError("MERGE_INVALID", "target 缺少 id", 422)
            if tid not in sources and tid in new_state:
                raise StoreError(
                    "CONTAINER_EXISTS",
                    f"目标桶 {tid} 已存在且不在源桶中；合并会覆盖", 409)
            full_target = {"id": tid,
                           "material": target.get(
                               "material", src_objs[0].get("material")),
                           "capacity_l": target.get("capacity_l"),
                           "position": target.get(
                               "position", src_objs[0].get("position"))}
            if full_target["capacity_l"] is None:
                raise StoreError("MERGE_INVALID",
                                 "target 需要 capacity_l", 422)
            merged = engine.merge_containers(full_target, src_objs)
            merged["merged_from"] = list(sources)
            for s in sources:
                new_state.pop(s, None)
            new_state[tid] = merged

        return new_state

    @staticmethod
    def _require_container_fields(c: Any):
        if not isinstance(c, dict) or not c.get("id"):
            raise StoreError("CONTAINER_INVALID",
                             "容器对象缺少 id", 422)
        for f in ("components", "material", "capacity_l", "volume_l"):
            if f not in c:
                raise StoreError("CONTAINER_INVALID",
                                 f"容器 {c.get('id')} 缺少字段 {f}", 422)
        if not isinstance(c["components"], list) or not c["components"]:
            raise StoreError("CONTAINER_INVALID",
                             f"容器 {c['id']} components 必须是非空列表", 422)
        for comp in c["components"]:
            if not isinstance(comp, dict) or "name" not in comp or \
                    "conc_min" not in comp or "conc_max" not in comp:
                raise StoreError(
                    "CONTAINER_INVALID",
                    f"容器 {c['id']} 成分项需含 name/conc_min/conc_max", 422)
            try:
                lo, hi = float(comp["conc_min"]), float(comp["conc_max"])
            except (TypeError, ValueError):
                raise StoreError(
                    "CONTAINER_INVALID",
                    f"容器 {c['id']} 成分 {comp.get('name')} 浓度非数值", 422)
            if lo < 0 or hi < 0 or lo > hi:
                raise StoreError(
                    "CONTAINER_INVALID",
                    f"容器 {c['id']} 成分 {comp.get('name')} "
                    f"浓度范围非法 [{lo},{hi}]", 422)
        try:
            cap, vol = float(c["capacity_l"]), float(c["volume_l"])
        except (TypeError, ValueError):
            raise StoreError(
                "CONTAINER_INVALID",
                f"容器 {c['id']} capacity_l/volume_l 非数值", 422)
        if cap <= 0 or vol < 0:
            raise StoreError(
                "CONTAINER_INVALID",
                f"容器 {c['id']} 容量须>0、装量须>=0", 422)

    def replay_state(self, facility_id: Optional[str] = None
                     ) -> Dict[str, Dict[str, Any]]:
        """从事件日志第 0 条全量回放，得到当前容器状态（权威重放）。

        不传 facility_id 时回放该设施以外的全部事件（兼容内部调用时由调用方
        显式传入）；正常路径始终按设施隔离回放。
        """
        if facility_id is not None:
            rows = self.conn.execute(
                "SELECT * FROM events WHERE facility_id=? ORDER BY seq",
                (facility_id,)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM events ORDER BY seq").fetchall()
        state: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            payload = json.loads(row["payload"])
            state = self._apply_event(
                row["type"], row["container_id"], payload, state)
        # 清掉内部标记不影响：state 即计算输入
        return state

    def list_events(self, facility_id: Optional[str] = None
                    ) -> List[Dict[str, Any]]:
        if facility_id is not None:
            rows = self.conn.execute(
                "SELECT seq, event_id, ts, type, container_id, payload,"
                " facility_id, matrix_version FROM events"
                " WHERE facility_id=? ORDER BY seq",
                (facility_id,)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT seq, event_id, ts, type, container_id, payload,"
                " facility_id, matrix_version FROM events ORDER BY seq"
            ).fetchall()
        return [{"seq": r["seq"], "event_id": r["event_id"], "ts": r["ts"],
                 "type": r["type"], "container_id": r["container_id"],
                 "payload": json.loads(r["payload"]),
                 "facility_id": r["facility_id"],
                 "matrix_version": r["matrix_version"]} for r in rows]

    # ------------------------------------------------------------------ #
    # 修订
    # ------------------------------------------------------------------ #
    def _create_revision(self, state: Dict[str, Dict[str, Any]],
                         facility: Dict[str, Any], matrix: Dict[str, Any],
                         kind: str, trigger_seq: Optional[int] = None,
                         trigger_event_id: Optional[str] = None,
                         created_ts: Optional[str] = None
                         ) -> Dict[str, Any]:
        fac_clean = {k: v for k, v in facility.items()
                     if not k.startswith("_")}
        containers = [state[k] for k in sorted(state)]
        result = engine.evaluate_layout(matrix, fac_clean, containers)

        with self.lock, self.conn:
            last = self.conn.execute(
                "SELECT rev_id FROM revisions WHERE facility_id=?"
                " ORDER BY rowid DESC LIMIT 1",
                (facility["id"],)).fetchone()
            parent = last["rev_id"] if last else None
            rev_id = new_id("rev")
            self.conn.execute(
                "INSERT INTO revisions(rev_id, parent_rev, facility_id,"
                " facility_version, matrix_version, matrix_fingerprint,"
                " kind, trigger_event_seq, trigger_event_id, state, result,"
                " disposition, created_ts, created_seq)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rev_id, parent, facility["id"], facility["_version"],
                 matrix["version"], matrix["_fingerprint"], kind,
                 trigger_seq, trigger_event_id,
                 json.dumps(state, ensure_ascii=False),
                 json.dumps(result, ensure_ascii=False),
                 result["disposition"], created_ts or now_iso(), trigger_seq))
        return {"revision_id": rev_id, "parent_revision_id": parent,
                "kind": kind, "trigger_event_id": trigger_event_id,
                "facility_version": facility["_version"],
                "matrix_version": matrix["version"],
                "matrix_fingerprint": matrix["_fingerprint"],
                "result": result}

    def latest_revision(self, facility_id: Optional[str] = None
                        ) -> Optional[Dict[str, Any]]:
        if facility_id is not None:
            row = self.conn.execute(
                "SELECT * FROM revisions WHERE facility_id=?"
                " ORDER BY rowid DESC LIMIT 1",
                (facility_id,)).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM revisions ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        return self._revision_row(row) if row else None

    def get_revision(self, rev_id: str) -> Dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM revisions WHERE rev_id=?", (rev_id,)).fetchone()
        if row is None:
            raise StoreError("REVISION_NOT_FOUND",
                             f"修订 {rev_id} 不存在", 404)
        return self._revision_row(row)

    def _revision_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        confirmed = self.conn.execute(
            "SELECT id, confirmed_at, note, trial_id FROM confirmations"
            " WHERE rev_id=? ORDER BY rowid", (row["rev_id"],)).fetchall()
        return {
            "revision_id": row["rev_id"],
            "parent_revision_id": row["parent_rev"],
            "kind": row["kind"],
            "facility_id": row["facility_id"],
            "facility_version": row["facility_version"],
            "matrix_version": row["matrix_version"],
            "matrix_fingerprint": row["matrix_fingerprint"],
            "trigger_event_seq": row["trigger_event_seq"],
            "trigger_event_id": row["trigger_event_id"],
            "disposition": row["disposition"],
            "created_ts": row["created_ts"],
            "state": json.loads(row["state"]),
            "result": json.loads(row["result"]),
            "confirmations": [dict(c) for c in confirmed],
        }

    def list_revisions(self, facility_id: Optional[str] = None
                       ) -> List[Dict[str, Any]]:
        if facility_id is not None:
            rows = self.conn.execute(
                "SELECT rev_id, parent_rev, kind, matrix_version,"
                " matrix_fingerprint, disposition, created_ts,"
                " trigger_event_id, facility_version FROM revisions"
                " WHERE facility_id=? ORDER BY rowid",
                (facility_id,)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT rev_id, parent_rev, kind, matrix_version,"
                " matrix_fingerprint, disposition, created_ts,"
                " trigger_event_id, facility_version FROM revisions"
                " ORDER BY rowid").fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # 试排 / 移动预检（不落事件，冻结计算结果）
    # ------------------------------------------------------------------ #
    def save_trial(self, kind: str, proposal: Dict[str, Any],
                   facility: Dict[str, Any], matrix: Dict[str, Any],
                   state: Dict[str, Dict[str, Any]],
                   base_rev: Optional[str],
                   result: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        fac_clean = {k: v for k, v in facility.items()
                     if not k.startswith("_")}
        if result is None:
            containers = [state[k] for k in sorted(state)]
            result = engine.evaluate_layout(matrix, fac_clean, containers)
        trial_id = new_id("trial")
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO trials(trial_id, kind, base_rev, facility_id,"
                " matrix_version, matrix_fingerprint, proposal, state,"
                " result, disposition, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (trial_id, kind, base_rev, facility["id"],
                 matrix["version"], matrix["_fingerprint"],
                 json.dumps(proposal, ensure_ascii=False),
                 json.dumps(state, ensure_ascii=False),
                 json.dumps(result, ensure_ascii=False),
                 result["disposition"], now_iso()))
        return self.get_trial(trial_id)

    def get_trial(self, trial_id: str) -> Dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM trials WHERE trial_id=?",
            (trial_id,)).fetchone()
        if row is None:
            raise StoreError("TRIAL_NOT_FOUND",
                             f"试排记录 {trial_id} 不存在", 404)
        return {
            "trial_id": row["trial_id"],
            "kind": row["kind"],
            "base_rev": row["base_rev"],
            "facility_id": row["facility_id"],
            "matrix_version": row["matrix_version"],
            "matrix_fingerprint": row["matrix_fingerprint"],
            "proposal": json.loads(row["proposal"]),
            "state": json.loads(row["state"]),
            "result": json.loads(row["result"]),
            "disposition": row["disposition"],
            "created_at": row["created_at"],
        }

    # ------------------------------------------------------------------ #
    # 确认（不回写旧修订，新增确认记录）
    # ------------------------------------------------------------------ #
    def confirm(self, rev_id: str, note: Optional[str] = None,
                trial_id: Optional[str] = None) -> Dict[str, Any]:
        rev = self.get_revision(rev_id)
        if rev["disposition"] != "disposable":
            raise StoreError(
                "CONFIRM_BLOCKED",
                f"修订 {rev_id} 仍为待处置（pending），"
                f"存在 {rev['result']['summary']['blockers']} 项阻断问题，"
                "禁止确认", 409,
                {"blockers": [{"code": i["code"], "message": i["message"],
                               "containers": i["containers"],
                               "positions": i["positions"],
                               "basis": i["basis"],
                               "severity": i["severity"]}
                              for i in rev["result"]["issues"]
                              if i["blocking"]]})
        if trial_id is not None:
            trial = self.get_trial(trial_id)
            if trial["matrix_fingerprint"] != rev["matrix_fingerprint"]:
                raise StoreError(
                    "CONFIRM_MATRIX_MISMATCH",
                    "试排与修订使用的冻结矩阵不一致，禁止确认", 409)
            if trial["result"]["fingerprint"] != rev["result"]["fingerprint"]:
                raise StoreError(
                    "CONFIRM_LAYOUT_MISMATCH",
                    "试排布局与修订布局复算指纹不一致，禁止确认", 409)
        cid = new_id("cfm")
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO confirmations(id, rev_id, trial_id,"
                " matrix_version, confirmed_at, note)"
                " VALUES(?,?,?,?,?,?)",
                (cid, rev_id, trial_id, rev["matrix_version"],
                 now_iso(), note))
        return self.get_revision(rev_id)
