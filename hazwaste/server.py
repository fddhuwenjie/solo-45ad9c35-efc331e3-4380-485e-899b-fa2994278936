"""危险废液暂存隔离复核 API（仅依赖标准库 http.server / json / sqlite3）。

启动:
    python -m hazwaste.server [--host 127.0.0.1] [--port 8080] [--db path.db]

端点:
    GET  /health
    # 冻结矩阵
    GET  /matrices
    GET  /matrices/{ver}
    POST /matrices                      冻结新版本（已存在则内容必须一致）
    GET  /matrices/{old}/diff/{new}     矩阵版本比较
    # 设施（柜体/分区/托盘/通风），版本化
    PUT  /facilities/{fid}
    GET  /facilities/{fid}?version=
    # 事件（入库/移位/合并/成分更正/换桶），每次产生不可改写的新修订
    POST /facilities/{fid}/events
    GET  /events
    # 布局复核
    POST /facilities/{fid}/layout/try         试排（不落事件，结果冻结）
    POST /facilities/{fid}/layout/move-check  移动预检（不落事件，结果冻结）
    # 修订
    GET  /revisions
    GET  /revisions/latest
    GET  /revisions/{rid}
    GET  /revisions/{rid}/disposal?container_id=C  逐桶处置单（计算明细）
    POST /revisions/{rid}/recalc?container_id=C    复算 JSON（重算并核对指纹）
    GET  /revisions/{a}/diff/{b}                   修订版本比较
    POST /revisions/{rid}/confirm                  确认（只能确认无阻断项的修订）
    GET  /trials/{tid}
"""
from __future__ import annotations

import argparse
import copy
import json
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

from . import engine
from .report import build_disposal_sheet
from .storage import Store, StoreError


class Api:
    def __init__(self, store: Store):
        self.store = store

    # ------------------------------------------------------------------ #
    def health(self, q):
        latest = None
        try:
            latest = self.store.latest_matrix_version()
        except StoreError:
            pass
        return {"status": "ok", "service": "hazwaste-isolation-api",
                "latest_frozen_matrix": latest,
                "matrices": [m["version"] for m in self.store.list_matrices()]}

    def resolve_matrix(self, body: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        ver = (body or {}).get("matrix_version") or \
              self.store.latest_matrix_version()
        return self.store.load_matrix(ver)

    # ---- 矩阵 ---------------------------------------------------------- #
    def list_matrices(self, q):
        return {"matrices": self.store.list_matrices()}

    def get_matrix(self, ver, q):
        return self.store.load_matrix(ver)

    def freeze_matrix(self, body, q):
        return self.store.freeze_matrix(body, source="uploaded")

    def diff_matrix(self, ver, new, q):
        return engine.diff_matrices(self.store.load_matrix(ver),
                                    self.store.load_matrix(new))

    # ---- 设施 ---------------------------------------------------------- #
    def put_facility(self, fid, body, q):
        body = copy.deepcopy(body)
        body["id"] = fid
        return self.store.put_facility(fid, body)

    def get_facility(self, fid, q):
        version = q.get("version", [None])[0]
        return self.store.get_facility(
            fid, int(version) if version is not None else None)

    # ---- 事件 ---------------------------------------------------------- #
    def post_event(self, fid, body, q):
        facility = self.store.get_facility(fid)
        matrix = self.resolve_matrix(body)
        events = body.get("events")
        single = body.get("event")
        if events is None and single is None and "type" in body:
            single = body
        if events is None:
            events = [single] if single else []
        if not events:
            raise StoreError("NO_EVENT",
                             "请求需包含 event 或 events 列表", 422)

        revisions = []
        last_ts = None
        for ev in events:
            rev = self.store.append_event(
                ev, fid, matrix["version"],
                expected_after_ts=last_ts)
            last_ts = ev["ts"]
            revisions.append(rev)
        return {"applied": len(revisions),
                "facility_id": fid,
                "matrix_version": matrix["version"],
                "matrix_fingerprint": matrix["_fingerprint"],
                "revisions": [self._rev_brief(r) for r in revisions],
                "latest": revisions[-1]["result"]["disposition"]}

    def list_events(self, q):
        fid = q.get("facility_id", [None])[0]
        return {"events": self.store.list_events(fid)}

    # ---- 试排 / 移动预检 ----------------------------------------------- #
    def _hypothetical_state(self, fid: str, proposal: Dict[str, Any]
                            ) -> Dict[str, Dict[str, Any]]:
        """在当前事件重放状态上，不落库地套用提议动作。"""
        state = self.store.replay_state(fid)
        for mv in proposal.get("moves", []):
            cid = mv.get("container_id")
            if cid not in state:
                raise StoreError("CONTAINER_NOT_FOUND",
                                 f"容器 {cid} 不存在", 404)
            if "position" not in mv:
                raise StoreError("TRIAL_INVALID",
                                 "moves 项需要 position", 422)
            state[cid]["position"] = copy.deepcopy(mv["position"])
        for mg in proposal.get("merges", []):
            state = self.store._apply_event(
                "merge", None, mg, state)
        for rep in proposal.get("repacks", []):
            state = self.store._apply_event(
                "repack", rep.get("container_id"),
                rep.get("payload", rep), state)
        return state

    def trial(self, fid, body, q):
        facility = self.store.get_facility(fid)
        matrix = self.resolve_matrix(body)
        proposal = body.get("proposal", {})
        state = self._hypothetical_state(fid, proposal)
        base = self.store.latest_revision(fid)
        saved = self.store.save_trial(
            "trial", proposal, facility, matrix, state,
            base["revision_id"] if base else None)
        return self._trial_view(saved, base)

    def move_check(self, fid, body, q):
        facility = self.store.get_facility(fid)
        matrix = self.resolve_matrix(body)
        cid = body.get("container_id")
        position = body.get("position")
        if not cid or position is None:
            raise StoreError("MOVECHECK_INVALID",
                             "需要 container_id 与 position", 422)
        proposal = {"moves": [{"container_id": cid, "position": position}]}
        state = self._hypothetical_state(fid, proposal)
        base = self.store.latest_revision(fid)
        saved = self.store.save_trial(
            "move_check", proposal, facility, matrix, state,
            base["revision_id"] if base else None)
        view = self._trial_view(saved, base)
        view["container_id"] = cid
        view["target_position"] = position
        # 预检结论：移动是否引入/消除阻断项
        view["move_verdict"] = self._move_verdict(base, saved)
        return view

    @staticmethod
    def _move_verdict(base: Optional[Dict[str, Any]],
                      trial: Dict[str, Any]) -> Dict[str, Any]:
        result = trial["result"]
        blockers = [i for i in result["issues"] if i["blocking"]]
        if base is None:
            return {"allowed": not blockers,
                    "reason": ("当前无历史修订，按试排结果判定"
                               if not blockers else "存在阻断项")}
        cmp = engine.diff_results(base["result"], result)
        introduced = cmp["issues_introduced"]
        if introduced:
            return {
                "allowed": False,
                "reason": "移动将引入禁配/盛漏等阻断问题",
                "issues_introduced": introduced,
                "issues_resolved": cmp["issues_resolved"],
            }
        if not blockers:
            return {"allowed": True,
                    "reason": "移动后全部核查通过",
                    "issues_resolved": cmp["issues_resolved"]}
        return {"allowed": False,
                "reason": "移动未引入新问题，但布局中仍存在阻断项",
                "issues_persisted": cmp["issues_persisted"]}

    def _trial_view(self, saved: Dict[str, Any],
                    base: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        view = {k: saved[k] for k in (
            "trial_id", "kind", "base_rev", "matrix_version",
            "matrix_fingerprint", "proposal", "disposition", "created_at")}
        view["result"] = saved["result"]
        view["frozen"] = True
        if base is not None:
            view["diff_vs_base"] = engine.diff_results(
                base["result"], saved["result"])
        return view

    # ---- 修订 ---------------------------------------------------------- #
    def list_revisions(self, q):
        fid = q.get("facility_id", [None])[0]
        return {"revisions": self.store.list_revisions(fid)}

    def latest_revision(self, q):
        fid = q.get("facility_id", [None])[0]
        rev = self.store.latest_revision(fid)
        if rev is None:
            raise StoreError("NO_REVISION", "尚无任何修订", 404)
        return rev

    def get_revision(self, rid, q):
        return self.store.get_revision(rid)

    def confirm_revision(self, rid, body, q):
        body = body or {}
        return self.store.confirm(
            rid, note=body.get("note"),
            trial_id=body.get("trial_id"))

    def diff_revision(self, a, b, q):
        ra = self.store.get_revision(a)
        rb = self.store.get_revision(b)
        out = engine.diff_results(ra["result"], rb["result"])
        out["revisions"] = {"old": a, "new": b}
        return out

    def disposal_sheet(self, rid, q):
        rev = self.store.get_revision(rid)
        cid = q.get("container_id", [None])[0]
        return build_disposal_sheet(rev, cid)

    def recalc(self, rid, body, q):
        rev = self.store.get_revision(rid)
        matrix = self.store.load_matrix(rev["matrix_version"])
        if matrix["_fingerprint"] != rev["matrix_fingerprint"]:
            raise StoreError("MATRIX_TAMPERED",
                             "冻结矩阵指纹与修订记录不一致", 409)
        facility = self.store.get_facility(
            rev["facility_id"], rev["facility_version"])
        fac_clean = {k: v for k, v in facility.items()
                     if not k.startswith("_")}
        containers = [rev["state"][k] for k in sorted(rev["state"])]
        re_result = engine.evaluate_layout(matrix, fac_clean, containers)
        stored = rev["result"]
        cid = q.get("container_id", [None])[0]
        verified = re_result["fingerprint"] == stored["fingerprint"]
        out = {
            "revision_id": rid,
            "matrix_version": rev["matrix_version"],
            "matrix_fingerprint": rev["matrix_fingerprint"],
            "facility_id": rev["facility_id"],
            "facility_version": rev["facility_version"],
            "inputs": {"facility": fac_clean, "containers": containers},
            "limits": matrix["limits"],
            "result": re_result,
            "stored_fingerprint": stored["fingerprint"],
            "recalculated_fingerprint": re_result["fingerprint"],
            "verified": verified,
            "immutable_stored_result": stored if not verified else
                "(与重算一致，已省略以减小响应；如需完整旧结果请 GET 该修订)",
        }
        if cid is not None:
            if cid not in rev["state"]:
                raise StoreError("CONTAINER_NOT_FOUND",
                                 f"修订中不存在容器 {cid}", 404)
            out["container_detail"] = build_disposal_sheet(rev, cid)
        return out

    def get_trial(self, tid, q):
        return self.store.get_trial(tid)

    @staticmethod
    def _rev_brief(rev: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "revision_id": rev["revision_id"],
            "parent_revision_id": rev["parent_revision_id"],
            "kind": rev["kind"],
            "trigger_event_id": rev["trigger_event_id"],
            "matrix_version": rev["matrix_version"],
            "matrix_fingerprint": rev["matrix_fingerprint"],
            "disposition": rev["result"]["disposition"],
            "blockers": rev["result"]["summary"]["blockers"],
            "warnings": rev["result"]["summary"]["warnings"],
            "fingerprint": rev["result"]["fingerprint"],
        }


# --------------------------------------------------------------------------- #
# HTTP 路由
# --------------------------------------------------------------------------- #
ROUTES = [
    ("GET",    "/health",                                   "health"),
    ("GET",    "/matrices",                                 "list_matrices"),
    ("POST",   "/matrices",                                 "freeze_matrix"),
    ("GET",    r"/matrices/{ver}/diff/{new}",               "diff_matrix"),
    ("GET",    r"/matrices/{ver}",                          "get_matrix"),
    ("GET",    "/events",                                   "list_events"),
    ("PUT",    r"/facilities/{fid}",                        "put_facility"),
    ("GET",    r"/facilities/{fid}",                        "get_facility"),
    ("POST",   r"/facilities/{fid}/events",                 "post_event"),
    ("POST",   r"/facilities/{fid}/layout/try",             "trial"),
    ("POST",   r"/facilities/{fid}/layout/move-check",      "move_check"),
    ("GET",    "/revisions",                                "list_revisions"),
    ("GET",    "/revisions/latest",                         "latest_revision"),
    ("POST",   r"/revisions/{rid}/confirm",                 "confirm_revision"),
    ("POST",   r"/revisions/{rid}/recalc",                  "recalc"),
    ("GET",    r"/revisions/{rid}/disposal",                "disposal_sheet"),
    ("GET",    r"/revisions/{a}/diff/{b}",                  "diff_revision"),
    ("GET",    r"/revisions/{rid}",                         "get_revision"),
    ("GET",    r"/trials/{tid}",                            "get_trial"),
]


def _match_route(method: str, path: str):
    for m, pattern, name in ROUTES:
        if m != method:
            continue
        if "{" not in pattern:
            if path == pattern:
                return name, {}
            continue
        p_parts = pattern.strip("/").split("/")
        u_parts = path.strip("/").split("/")
        if len(p_parts) != len(u_parts):
            continue
        params: Dict[str, str] = {}
        ok = True
        for pp, uu in zip(p_parts, u_parts):
            if pp.startswith("{") and pp.endswith("}"):
                params[pp[1:-1]] = uu
            elif pp != uu:
                ok = False
                break
        if ok:
            return name, params
    return None, None


class Handler(BaseHTTPRequestHandler):
    server_version = "HazwasteIsolationAPI/1.0"

    def log_message(self, fmt, *args):  # 静默常规访问日志
        pass

    def _send_json(self, obj: Any, status: int = 200):
        data = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> Optional[Dict[str, Any]]:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StoreError("BAD_JSON", f"请求体不是合法 JSON: {exc}", 400)
        if not isinstance(body, dict):
            raise StoreError("BAD_BODY", "请求体必须是 JSON 对象", 400)
        return body

    def _dispatch(self, method: str):
        parts = urlsplit(self.path)
        qs = parse_qs(parts.query)
        name, params = _match_route(method, parts.path)
        if name is None:
            self._send_json(
                {"error": {"code": "NOT_FOUND",
                           "message": f"无此端点: {method} {parts.path}"}}, 404)
            return
        api: Api = self.server.api  # type: ignore
        try:
            body = self._read_body() if method in ("POST", "PUT") else None
            fn = getattr(api, name)
            sig = fn.__code__.co_varnames[:fn.__code__.co_argcount]
            kwargs = dict(params)
            if "body" in sig:
                kwargs["body"] = body
            if "q" in sig:
                kwargs["q"] = qs
            out = fn(**kwargs)
            status = 201 if method == "POST" and name in (
                "post_event", "freeze_matrix", "trial", "move_check",
                "confirm_revision") else 200
            self._send_json(out, status)
        except StoreError as exc:
            self._send_json({"error": {"code": exc.code,
                                       "message": str(exc),
                                       "detail": exc.detail}}, exc.status)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._send_json(
                {"error": {"code": "INTERNAL", "message": str(exc),
                           "trace": traceback.format_exc()}}, 500)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")


def make_server(host: str, port: int, db_path: str
                ) -> Tuple[ThreadingHTTPServer, Store]:
    store = Store(db_path)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.api = Api(store)  # type: ignore[attr-defined]
    httpd.store = store     # type: ignore[attr-defined]
    return httpd, store


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="危险废液暂存隔离复核 API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", default=":memory:",
                        help="sqlite3 文件路径（默认 :memory:，重启即清空）")
    args = parser.parse_args(argv)
    httpd, _store = make_server(args.host, args.port, args.db)
    print(f"危险废液暂存隔离复核 API 监听 http://{args.host}:{args.port}"
          f"（db={args.db}）", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
