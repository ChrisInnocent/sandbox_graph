"""Product-specific control/data-plane mappings for the audited checkouts."""
import base64
import copy
import json
import math
import os
import re
import time
from urllib.parse import quote, urlencode

from .transport import (TransportError, decode_json, encode_connect, parse_connect,
                        remaining, request, validate_url, ConnectDecoder)


def _sandbox(value, response=None):
    if not isinstance(value, dict) or not isinstance(value.get("sandboxID"), str) or not value["sandboxID"]:
        raise ValueError("sandbox response missing identifier")
    result = {"sandbox_id": value["sandboxID"], "private": value}
    if response is not None:
        result.update(api_headers_s=response.headers_s, api_complete_s=response.complete_s)
    return result


def _kuasar_source(source):
    value, kind = source["id"], source["kind"]
    if re.fullmatch(r"(?:e2b|bare)-(?:img|sbx|snp)-[A-Za-z0-9_-]+", value):
        return value  # Already encoded platform identifier; server validates it.
    suffix = {"image": "img", "snapshot": "snp"}.get(kind)
    if suffix is None:
        raise ValueError("Kuasar template requires an encoded templateID")
    valid = re.fullmatch(r"manifest://[0-9a-f]{64}", value)
    file_ref = re.fullmatch(
        r"file://([^/@\\]+)(?:@(digest|hmac|manifest):[0-9a-f]{64})?"
        r"@location:[A-Za-z0-9][A-Za-z0-9_.-]*", value)
    if file_ref:
        ext = ".image" if kind == "image" else ".snapshot"
        valid = file_ref[1].endswith((ext, ".bundle")) and not any(
            ord(c) < 32 for c in file_ref[1])
    if not valid:
        raise ValueError("Kuasar source requires a canonical portable artifact reference")
    encoded = base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")
    return f"e2b-{suffix}-{encoded}"


class Adapter:
    def __init__(self, config):
        self.config = copy.deepcopy(config)
        self.product = config.get("product")
        if self.product not in ("agentenv", "cubesandbox", "kuasar"):
            raise ValueError("unsupported product")
        for key in ("api_url", "data_url"):
            validate_url(config.get(key, ""))
        self.api_url = config["api_url"].rstrip("/")
        self.data_url = config["data_url"].rstrip("/")
        self.headers = {"Content-Type": "application/json"}
        env = config.get("api_key_env")
        if env:
            secret = os.environ.get(env)
            if not secret:
                raise ValueError("API credential environment variable is missing")
            self.headers["X-API-Key"] = secret

    def probe_topology(self, topology, timeout):
        """Read-only creation-entry topology probe (BCD-13 §13.4).

        Never triggers create/build/capture. Absent explicit evidence the answer is
        None (unverified), never guessed from the topology or URL alone.
        """
        if topology not in ("router", "node_direct", "co_located_cli"):
            raise ValueError("unsupported topology")
        if topology == "co_located_cli":
            return {"topology": topology, "management_plane_present": False,
                    "evidence": "co_located_no_control_plane"}
        declared = self.config.get("management_plane_present")
        if declared is None:
            return {"topology": topology, "management_plane_present": None,
                    "evidence": "undeclared"}
        if not isinstance(declared, bool):
            raise ValueError("management_plane_present must be boolean")
        return {"topology": topology, "management_plane_present": declared,
                "evidence": "declared"}

    def _control(self, method, path, timeout, body=None):
        encoded = None if body is None else json.dumps(body, allow_nan=False).encode()
        return request(method, self.api_url + path, body=encoded,
                       headers=self.headers, timeout=timeout)

    def create(self, run_id, timeout):
        source = self.config["source"]
        if source.get("kind") not in ("template", "snapshot", "image") or not isinstance(source.get("id"), str) or not source["id"]:
            raise ValueError("invalid source")
        if self.product == "cubesandbox" and source["kind"] == "image":
            raise NotImplementedError("Cube image-to-template build path is not implemented")
        body = copy.deepcopy(self.config.get("create_body", {}))
        if not isinstance(body, dict) or not isinstance(body.get("metadata", {}), dict):
            raise ValueError("invalid create body")
        body["metadata"] = {**body.get("metadata", {}), "run_id": run_id}
        path = "/sandboxes"
        if self.product == "agentenv" and source["kind"] == "image":
            path = "/sandboxes-cold"
            body.pop("templateID", None)
            body["image"] = source["id"]
        else:
            body.pop("image", None)
            body["templateID"] = _kuasar_source(source) if self.product == "kuasar" else source["id"]
        response = self._control("POST", path, timeout, body)
        return _sandbox(decode_json(response.body), response)

    def info(self, sandbox, timeout):
        path = "/sandboxes/" + quote(sandbox["sandbox_id"], safe="")
        response = self._control("GET", path, timeout)
        data = decode_json(response.body)
        if not isinstance(data, dict):
            raise ValueError("invalid sandbox detail")
        sandbox.setdefault("private", {}).update(data)
        return data

    def execute(self, sandbox, command, timeout):
        private = sandbox.get("private", {})
        sid = sandbox["sandbox_id"]
        headers = {"Content-Type": "application/connect+json",
                   "Connect-Protocol-Version": "1",
                   "Connect-Timeout-Ms": str(max(1, int(timeout * 1000)))}
        user = self.config.get("envd_user", "root")
        if not isinstance(user, str) or ":" in user or not user:
            raise ValueError("invalid envd user")
        headers["Authorization"] = "Basic " + base64.b64encode((user + ":").encode()).decode()
        if private.get("envdAccessToken"):
            headers["X-Access-Token"] = private["envdAccessToken"]
        if self.product == "agentenv":
            headers.update({"x-agentenv-sandbox-id": sid, "x-agentenv-target-port": "49983"})
        elif self.product == "kuasar":
            headers.update({"E2b-Sandbox-Id": sid, "E2b-Sandbox-Port": "49983"})
        else:
            domain = self.config.get("domain") or private.get("domain")
            if not isinstance(domain, str) or not re.fullmatch(r"[A-Za-z0-9.-]+(?::[0-9]+)?", domain):
                raise ValueError("Cube data-plane domain is required")
            headers["Host"] = f"49983-{sid}.{domain}"
            if private.get("trafficAccessToken"):
                headers["e2b-traffic-access-token"] = private["trafficAccessToken"]
        started = time.monotonic()
        boundaries = {}
        def observe_event(kind):
            boundaries.setdefault(kind, time.monotonic() - started)
        decoder = ConnectDecoder(on_event=observe_event)
        response = request("POST", self.data_url + "/process.Process/Start",
                           body=encode_connect({"process": {"cmd": "/bin/sh", "args": ["-c", command]},
                                                "stdin": False}),
                           headers=headers, timeout=timeout, on_chunk=decoder.feed)
        content_type = response.headers.get("content-type", "").split(";")[0].strip()
        if content_type != "application/connect+json":
            raise TransportError("unexpected command response content type")
        if not decoder.total:
            decoder.feed(response.body)
        result = decoder.finish()
        result["event_offsets_s"] = boundaries
        result["api_complete_s"] = getattr(response, "complete_s", time.monotonic() - started)
        return result

    def delete(self, sandbox, timeout):
        self._control("DELETE", "/sandboxes/" + quote(sandbox["sandbox_id"], safe=""), timeout)

    def list_owned(self, run_id, timeout):
        deadline = time.monotonic() + timeout
        result, ids, cursors = [], set(), set()
        query = {"limit": 100}
        if self.product != "kuasar":
            query["metadata"] = ("run_id=" + run_id if self.product == "cubesandbox"
                                 else urlencode({"run_id": run_id}))
        while True:
            response = self._control("GET", "/v2/sandboxes?" + urlencode(query), remaining(deadline))
            data = decode_json(response.body)
            if not isinstance(data, list):
                raise ValueError("invalid sandbox list")
            for row in data:
                if not isinstance(row, dict):
                    raise ValueError("invalid sandbox list entry")
                metadata = row.get("metadata") or {}
                if not isinstance(metadata, dict) or metadata.get("run_id") != run_id:
                    continue
                sandbox = _sandbox(row)
                if sandbox["sandbox_id"] not in ids:
                    ids.add(sandbox["sandbox_id"])
                    result.append(sandbox)
            cursor = response.headers.get("x-next-token")
            if self.product == "cubesandbox" or not cursor:
                return result
            if cursor in cursors:
                raise TransportError("repeated sandbox pagination cursor")
            cursors.add(cursor)
            query["nextToken"] = cursor

    def effective_spec(self, sandbox, timeout):
        data = self.info(sandbox, timeout)
        if self.product == "kuasar":
            # Public detail currently contains API-wide defaults, not per-instance evidence.
            return {"vcpus": None, "memory_mib": None}
        cpu = data.get("cpuCount")
        if self.product == "cubesandbox" and data.get("cpuMilli") is not None:
            cpu = data["cpuMilli"] / 1000 if type(data["cpuMilli"]) in (int, float) else None
        def valid(value):
            return value if type(value) in (int, float) and math.isfinite(value) and value > 0 else None
        return {"vcpus": valid(cpu), "memory_mib": valid(data.get("memoryMB"))}

    def inspect(self, sandbox, timeout):
        """Only the detail endpoint: connect/data-plane can wake a paused VM."""
        data = self.info(sandbox, timeout)
        state = data.get("state", "unknown")
        return {"sandbox_id": sandbox["sandbox_id"], "state": state,
                "raw": {k: v for k, v in data.items() if "token" not in k.lower()}}

    def pause(self, sandbox, timeout, *, memory=True):
        path = "/sandboxes/" + quote(sandbox["sandbox_id"], safe="") + "/pause"
        body = {"memory": memory} if self.product == "kuasar" else None
        response = self._control("POST", path, timeout, body)
        return {"api_headers_s": response.headers_s, "api_complete_s": response.complete_s,
                "memory_requested": memory, "completion": "api_only"}

    def resume(self, sandbox, timeout, *, memory=True, ttl=None):
        action = "connect" if self.product == "kuasar" else "resume"
        body = {} if ttl is None else {"timeout": ttl}
        if self.product == "kuasar": body["memory"] = memory
        response = self._control("POST", "/sandboxes/" + quote(sandbox["sandbox_id"], safe="") + "/" + action,
                                 timeout, body)
        result = _sandbox(decode_json(response.body), response)
        if result["sandbox_id"] != sandbox["sandbox_id"]:
            raise ValueError("resume changed sandbox identity")
        sandbox.setdefault("private", {}).update(result["private"])
        return result

    def renew(self, sandbox, ttl, timeout):
        if type(ttl) is not int or ttl <= 0: raise ValueError("positive TTL required")
        response = self._control("POST", "/sandboxes/" + quote(sandbox["sandbox_id"], safe="") + "/timeout",
                                 timeout, {"timeout": ttl})
        return {"api_complete_s": response.complete_s, "ttl_from_now_s": ttl}

    def fork(self, sandbox, count, timeout):
        if self.product == "kuasar": raise NotImplementedError("Kuasar clone uses pause/export/create")
        if type(count) is not int or not 1 <= count <= 100: raise ValueError("fork count outside API contract")
        body = {"count": count}
        if "timeout" in self.config.get("create_body", {}):
            body["timeout"] = self.config["create_body"]["timeout"]
        response = self._control("POST", "/sandboxes/" + quote(sandbox["sandbox_id"], safe="") + "/fork",
                                 timeout, body)
        rows = decode_json(response.body)
        if not isinstance(rows, list) or len(rows) != count: raise ValueError("incomplete fork response")
        results = []
        for row in rows:
            if not isinstance(row, dict) or set(row) not in ({"sandbox"}, {"error"}):
                raise ValueError("invalid fork item")
            if "sandbox" in row: results.append({"sandbox": _sandbox(row["sandbox"], response)})
            else:
                error = row["error"]
                if not isinstance(error, dict): raise ValueError("invalid fork error")
                results.append({"error": {"code": error.get("code", "unknown")}})
        return results

    def snapshot(self, sandbox, timeout, *, name=None):
        if self.product == "kuasar":
            raise NotImplementedError("Kuasar snapshot is explicit pause plus export, not a standalone call")
        body = {} if name is None else {"name": name}
        response = self._control("POST", "/sandboxes/" + quote(sandbox["sandbox_id"], safe="") + "/snapshots",
                                 timeout, body)
        value = decode_json(response.body)
        if not isinstance(value, dict) or not value.get("snapshotID"): raise ValueError("snapshot ID missing")
        return {"source_id": value["snapshotID"], "kind": "snapshot", "api_headers_s": response.headers_s,
                "api_complete_s": response.complete_s, "remote_status": value.get("remoteStatus"),
                "backend": value.get("backend"), "publication_verified": self.product == "agentenv"}

    def export_snapshot(self, sandbox, timeout):
        if self.product != "kuasar": return self.snapshot(sandbox, timeout)
        response = self._control("POST", "/sandboxes/" + quote(sandbox["sandbox_id"], safe="") + "/export",
                                 timeout, {"toTemplate": True, "keepSource": True})
        value = decode_json(response.body)
        if not isinstance(value, dict) or not isinstance(value.get("result"), str):
            raise ValueError("export source reference missing")
        return {"source_id": value["result"], "kind": "snapshot", "source_preserved": True,
                "source_state": "paused", "api_complete_s": response.complete_s}

    def delete_source(self, source_id, timeout):
        """Caller must establish exclusive ownership and zero remaining references."""
        response = self._control("DELETE", "/templates/" + quote(source_id, safe=""), timeout)
        return {"api_complete_s": response.complete_s, "physical_release": "unverified"}

    def build_image(self, run_id, timeout, poll_interval=1.0, *, handle=None, on_progress=None):
        """Single build path. A lost write response is never retried here.

        Caller persists on_progress before proceeding and reconciles known handles
        after a crash. A registered Kuasar handle with unknown trigger outcome must
        be inspected externally, not blindly triggered again.
        """
        if timeout <= 0 or poll_interval <= 0: raise ValueError("finite build timeouts required")
        if self.product == "agentenv":
            return {"source_id": self.config["source"]["id"], "kind": "image",
                    "build_in_create": True}
        started=time.monotonic();deadline=started+timeout
        notify=on_progress or (lambda row: None)
        if handle is None:
            body=copy.deepcopy(self.config.get("build_body", {}))
            if self.product == "cubesandbox":
                body["image"]=self.config["source"]["id"]
                notify({"phase":"intent","stage":"build_register"})
                response=self._control("POST", "/templates", remaining(deadline), body)
                row=decode_json(response.body)
                handle={"registration_id": row["templateID"], "build_id": row["jobID"], "triggered": True}
                notify({**handle,"phase":"receipt","stage":"build_register"})
            else:
                body.setdefault("name", "bench-"+run_id)
                notify({"phase":"intent","stage":"build_register"})
                response=self._control("POST", "/v3/templates", remaining(deadline), body)
                row=decode_json(response.body)
                handle={"registration_id": row["templateID"], "build_id": row["buildID"], "triggered": False}
                notify({**handle,"phase":"receipt","stage":"build_register"})
                path="/v2/templates/"+quote(handle["registration_id"],safe="")+"/builds/"+quote(handle["build_id"],safe="")
                notify({**handle,"phase":"intent","stage":"build_trigger"})
                self._control("POST",path,remaining(deadline),{"fromImage":self.config["source"]["id"]})
                handle["triggered"]=True;notify({**handle,"phase":"receipt","stage":"build_trigger"})
        elif not handle.get("triggered"):
            raise ValueError("unknown build trigger: reconcile before replay")
        path="/templates/"+quote(handle["registration_id"],safe="")+"/builds/"+quote(handle["build_id"],safe="")+"/status"
        while True:
            response=self._control("GET",path,remaining(deadline))
            state=decode_json(response.body)
            status=state.get("status")
            if status=="ready":
                sid=state.get("templateID") if self.product=="kuasar" else handle["registration_id"]
                if not isinstance(sid,str) or not sid: raise ValueError("persistent build result ID missing")
                if self.product=="kuasar" and (state.get("kind")!="img" or not sid.startswith("e2b-img-")):
                    raise ValueError("builder result is not the requested image profile")
                result={**handle,"source_id":sid,"kind":"template","build_elapsed_s":time.monotonic()-started}
                notify({**result,"phase":"observed","stage":"build_ready"});return result
            if status=="error":raise ValueError("image build failed")
            if status!="building":raise ValueError("unknown build status")
            time.sleep(min(poll_interval,remaining(deadline)))

    def resolve_source(self, declared_source, timeout):
        """只读来源身份解析（BCD-12 §12.4）。绝不触发 build/capture。

        无法从审计证据确认时返回 `resolution_status='unverified'`，不按参数名推断。
        """
        if not isinstance(declared_source, dict):
            raise ValueError("declared source must be a mapping")
        kind, sid = declared_source.get("kind"), declared_source.get("id")
        base = {"schema_version": "source.v2", "product": self.product,
                "product_version": str(self.config.get("product_version") or ""),
                "parameter_name": self.config.get("source_parameter", "source"),
                "declared_kind": kind, "artifact_id": sid,
                "artifact_digest": declared_source.get("digest"),
                "producer_ref": declared_source.get("producer_ref"),
                "path_evidence": f"docs/source-audit/{self.product}.md",
                "producer_step": "pre_existing", "spec_origin": "request",
                "resolution_status": "unverified"}
        if declared_source.get("artifact_kind") and declared_source.get("launch_path"):
            return {**base, "artifact_kind": declared_source["artifact_kind"],
                    "launch_path": declared_source["launch_path"],
                    "producer_step": declared_source.get("producer_step", "pre_existing"),
                    "resolution_status": "resolved"}
        if self.product == "agentenv":
            if kind in ("template", "snapshot"):
                return {**base, "artifact_kind": "snapshot_manifest", "launch_path": "memory_restore",
                        "spec_origin": "inherited", "resolution_status": "resolved"}
            if kind == "image":
                return {**base, "artifact_kind": "image_artifact", "launch_path": "cold_boot",
                        "resolution_status": "resolved"}
        if self.product == "kuasar" and kind == "template":
            from .source_resolution import kuasar_template_kind
            identity = kuasar_template_kind(sid)
            if identity:
                return {**base, "artifact_kind": identity[0], "launch_path": identity[1],
                        "resolution_status": "resolved"}
        # CubeSandbox 模板/快照同命名空间，只读接口无法区分；不得按名称猜测。
        return base

    def build_source(self, run_id, timeout, *, handle=None, on_progress=None):
        """B00 阶段机（BCD-12 §12.5）。丢失写响应不重试：先持久 handle，再对账。

        返回 {artifact:{kind,id,digest,size_bytes}, build_elapsed_s, terminal,
        cache_state, resource_peak, unresolved_external, handle}。
        """
        if type(timeout) not in (int, float) or timeout <= 0:
            raise ValueError("finite build timeouts required")
        if handle is not None and not handle.get("triggered"):
            return self._build_terminal(None, "unknown", 1, "build_trigger_unconfirmed", handle)
        if self.product == "agentenv":
            return self._build_snapshot_from_builder(run_id, timeout, handle=handle, on_progress=on_progress)
        try:
            built = self.build_image(run_id, timeout, self.config.get("poll_interval_s", 1.0),
                                     handle=handle, on_progress=on_progress)
        except (TimeoutError, TransportError) as error:
            return self._build_terminal(None, "unknown", 1, "build_unresolved_external", handle,
                                        type(error).__name__)
        except ValueError as error:
            return self._build_terminal(None, "error", 0, "build_failed", handle, type(error).__name__)
        artifact_kind = ("snapshot_manifest" if self.config.get("source", {}).get("kind") == "snapshot"
                         else "image_artifact")
        artifact = {"kind": artifact_kind, "id": built["source_id"],
                    "digest": built.get("digest"), "size_bytes": built.get("size_bytes")}
        record = {"registration_id": built.get("registration_id"), "build_id": built.get("build_id"),
                  "triggered": built.get("triggered", True)}
        return self._build_terminal(artifact, "ready", 0, None, record,
                                    build_elapsed_s=built.get("build_elapsed_s"),
                                    cache_state=built.get("cache_state", "unknown"),
                                    resource_peak=built.get("resource_peak"))

    def _build_snapshot_from_builder(self, run_id, timeout, *, handle=None, on_progress=None):
        """AgentENV 构建模板 = template_builder sandbox + publish_captured → 新 SnapshotId。"""
        builder = self.config.get("builder")
        if not isinstance(builder, dict) or builder.get("kind") not in ("template", "image") or not builder.get("id"):
            raise ValueError("agentenv build_source requires a configured builder source contract")
        notify = on_progress or (lambda row: None)
        run_adapter = Adapter({**self.config, "source": {"kind": builder["kind"], "id": builder["id"]}})
        notify({"phase": "intent", "stage": "build_register"})
        sandbox = run_adapter.create(run_id + ":builder", timeout)
        notify({"phase": "receipt", "stage": "build_register", "sandbox_id": sandbox["sandbox_id"]})
        try:
            captured = run_adapter.snapshot(sandbox, timeout)
        finally:
            try:
                run_adapter.delete(sandbox, timeout)
            except Exception:
                pass
        artifact = {"kind": "snapshot_manifest", "id": captured["source_id"],
                    "digest": None, "size_bytes": None}
        notify({"phase": "observed", "stage": "build_ready", **artifact})
        return self._build_terminal(artifact, "ready", 0, None,
                                    {"registration_id": captured["source_id"], "build_id": None,
                                     "triggered": True})

    def _build_terminal(self, artifact, terminal, unresolved, reason, handle, error_type=None,
                        build_elapsed_s=None, cache_state='unknown', resource_peak=None):
        return {"artifact": artifact, "terminal": terminal,
                "unresolved_external": unresolved, "reason": reason, "handle": handle,
                "build_elapsed_s": build_elapsed_s, "cache_state": cache_state,
                "resource_peak": resource_peak or {"pss_bytes": None, "cpu_seconds": None},
                "error_type": error_type}
