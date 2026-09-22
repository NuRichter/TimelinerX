"""Render graph: a DAG of checkpointed nodes (Section VII.2).

Each node declares its dependencies and a *fingerprint* (hash of the inputs
that affect its output). After a node succeeds, a checkpoint record
``nodes/<id>.json`` is written atomically with the fingerprint, duration and a
small JSON result. On resume, a node whose checkpoint exists with the same
fingerprint — and whose ``validate`` callback confirms its artefacts still
exist — is marked CACHED and skipped.

Independent nodes run concurrently on a thread pool; a node starts only when
all its dependencies are DONE/CACHED. A failure stops scheduling new nodes,
lets running nodes finish (or observe cancellation), and leaves every
completed node's checkpoint intact for resume.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..core.errors import TimelinerXError, RenderCancelledError, RenderFailedError
from ..utils.cancel import CancelToken


class NodeStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    CACHED = "cached"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


@dataclass
class Node:
    id: str
    fn: Callable[["NodeContext"], Any]
    deps: List[str] = field(default_factory=list)
    fingerprint: Callable[[], str] = lambda: ""
    validate: Optional[Callable[[Any], bool]] = None     # checkpoint artefacts still valid?
    weight: float = 1.0                                  # share of total progress
    label: str = ""
    checkpoint: bool = True


@dataclass
class NodeState:
    status: NodeStatus = NodeStatus.PENDING
    started: Optional[float] = None
    duration: Optional[float] = None
    progress: float = 0.0
    message: str = ""
    error: Optional[str] = None
    result: Any = None


class NodeContext:
    def __init__(self, graph: "RenderGraph", node: Node):
        self.graph = graph
        self.node = node
        self.cancel = graph.cancel
        self.job_dir = graph.job_dir

    def result(self, node_id: str) -> Any:
        return self.graph.states[node_id].result

    def progress(self, fraction: float, message: str = "") -> None:
        st = self.graph.states[self.node.id]
        st.progress = max(0.0, min(1.0, fraction))
        if message:
            st.message = message
        self.graph._emit("node_progress", self.node.id)

    def emit(self, kind: str, **data) -> None:
        self.graph._emit(kind, self.node.id, **data)


def fingerprint_of(*parts: Any) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(json.dumps(p, sort_keys=True, default=str).encode())
        h.update(b"\x1f")
    return h.hexdigest()[:32]


class RenderGraph:
    def __init__(self, job_dir: Path, cancel: CancelToken, listener: Optional[Callable[[dict], None]] = None,
                 max_workers: int = 3):
        self.job_dir = Path(job_dir)
        (self.job_dir / "nodes").mkdir(parents=True, exist_ok=True)
        self.cancel = cancel
        self.nodes: Dict[str, Node] = {}
        self.order: List[str] = []
        self.states: Dict[str, NodeState] = {}
        self.listener = listener
        self.max_workers = max_workers
        self._lock = threading.Lock()

    def add(self, node: Node) -> None:
        for d in node.deps:
            if d not in self.nodes:
                raise ValueError(f"Node {node.id} depends on unknown node {d}")
        self.nodes[node.id] = node
        self.order.append(node.id)
        self.states[node.id] = NodeState()

    # ------------------------------------------------------------- events
    def _emit(self, kind: str, node_id: Optional[str] = None, **data) -> None:
        if self.listener is None:
            return
        ev = {"event": kind, "time": time.time(), "overall": self.overall_progress()}
        if node_id:
            st = self.states[node_id]
            ev.update(node=node_id, status=st.status.value, progress=st.progress, message=st.message)
        ev.update(data)
        try:
            self.listener(ev)
        except Exception:  # noqa: BLE001 — a UI listener must not break rendering
            pass

    def overall_progress(self) -> float:
        tot = sum(n.weight for n in self.nodes.values()) or 1.0
        acc = 0.0
        for nid, n in self.nodes.items():
            st = self.states[nid]
            if st.status in (NodeStatus.DONE, NodeStatus.CACHED, NodeStatus.SKIPPED):
                acc += n.weight
            elif st.status == NodeStatus.RUNNING:
                acc += n.weight * st.progress
        return acc / tot

    # -------------------------------------------------------- checkpoints
    def _ckpt_path(self, nid: str) -> Path:
        return self.job_dir / "nodes" / f"{nid}.json"

    def _load_ckpt(self, node: Node) -> Optional[dict]:
        if not node.checkpoint:
            return None
        p = self._ckpt_path(node.id)
        if not p.is_file():
            return None
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if rec.get("fingerprint") != node.fingerprint() or rec.get("status") != "done":
            return None
        if rec.get("dep_digest") != self._dep_digest(node):
            return None
        if node.validate is not None and not node.validate(rec.get("result")):
            return None
        return rec

    def _save_ckpt(self, node: Node, st: NodeState) -> None:
        if not node.checkpoint:
            return
        rec = {"node": node.id, "status": "done", "fingerprint": node.fingerprint(),
               "dep_digest": self._dep_digest(node),
               "duration_s": st.duration, "finished_at": time.time(), "result": st.result}
        p = self._ckpt_path(node.id)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec, indent=1, default=str), encoding="utf-8")
        os.replace(tmp, p)

    def _dep_digest(self, node: Node) -> str:
        return fingerprint_of([self.states[d].result for d in node.deps])

    def invalidate(self, node_id: str) -> None:
        try:
            self._ckpt_path(node_id).unlink()
        except FileNotFoundError:
            pass

    # -------------------------------------------------------------- run
    def run(self) -> Dict[str, NodeState]:
        # topological validation
        indeg = {n: len(self.nodes[n].deps) for n in self.order}
        seen = [n for n in self.order if indeg[n] == 0]
        visited = set()
        while seen:
            n = seen.pop()
            visited.add(n)
            for m in self.order:
                if n in self.nodes[m].deps:
                    indeg[m] -= 1
                    if indeg[m] == 0:
                        seen.append(m)
        if len(visited) != len(self.order):
            raise ValueError("Render graph contains a cycle")

        # a node may be reused from checkpoint only if all its deps were too (or done this run)
        failure: Optional[BaseException] = None
        futures: Dict[Future, str] = {}
        with ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="tlx-node") as ex:
            while True:
                if failure is None and not self.cancel.cancelled:
                    for nid in self.order:
                        st = self.states[nid]
                        if st.status != NodeStatus.PENDING:
                            continue
                        node = self.nodes[nid]
                        dep_states = [self.states[d].status for d in node.deps]
                        if any(s not in (NodeStatus.DONE, NodeStatus.CACHED) for s in dep_states):
                            continue
                        rec = self._load_ckpt(node)
                        if rec is not None:
                            st.status = NodeStatus.CACHED
                            st.result = rec.get("result")
                            st.duration = rec.get("duration_s")
                            st.progress = 1.0
                            self._emit("node_cached", nid)
                            continue
                        st.status = NodeStatus.RUNNING
                        st.started = time.perf_counter()
                        self._emit("node_started", nid)
                        futures[ex.submit(self._run_node, node)] = nid
                if not futures:
                    break
                done, _ = wait(list(futures), return_when=FIRST_COMPLETED, timeout=0.5)
                for fut in done:
                    nid = futures.pop(fut)
                    st = self.states[nid]
                    st.duration = time.perf_counter() - (st.started or time.perf_counter())
                    exc = fut.exception()
                    if exc is None:
                        st.result = fut.result()
                        st.status = NodeStatus.DONE
                        st.progress = 1.0
                        self._save_ckpt(self.nodes[nid], st)
                        self._emit("node_done", nid, duration_s=st.duration)
                    else:
                        cancelled = isinstance(exc, RenderCancelledError)
                        st.status = NodeStatus.CANCELLED if cancelled else NodeStatus.FAILED
                        st.error = str(exc)
                        if not cancelled and not isinstance(exc, TimelinerXError):
                            st.error += "\n" + "".join(traceback.format_exception(exc))
                        self._emit("node_failed", nid, error=str(exc))
                        if failure is None:
                            failure = exc
                            if not cancelled:
                                # stop siblings promptly; their checkpoints of finished work remain
                                pass
        for nid in self.order:
            if self.states[nid].status == NodeStatus.PENDING:
                self.states[nid].status = NodeStatus.SKIPPED if failure is None else NodeStatus.PENDING
        if failure is not None:
            raise failure
        if self.cancel.cancelled:
            raise RenderCancelledError(self.cancel.reason or "cancelled")
        return self.states

    def _run_node(self, node: Node) -> Any:
        self.cancel.raise_if_cancelled()
        return node.fn(NodeContext(self, node))

    def snapshot(self) -> List[dict]:
        out = []
        for nid in self.order:
            st = self.states[nid]
            out.append({"node": nid, "label": self.nodes[nid].label or nid, "status": st.status.value,
                        "progress": round(st.progress, 4), "duration_s": st.duration,
                        "deps": self.nodes[nid].deps, "message": st.message, "error": st.error})
        return out
