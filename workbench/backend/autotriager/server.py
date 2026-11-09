"""Loopback API, built SPA hosting and durable event replay."""
from __future__ import annotations
import argparse
import hmac
import json
import mimetypes
import re
import sqlite3
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
from jsonschema import ValidationError
from .contracts import ROOT, example, validate
from .sessions import SessionManager
from .process_lock import ProcessLock

VERSION = "autotriager.api.v1"
TERMINAL = {"COMPLETED", "FAILED", "TIMED_OUT", "BUDGET_EXHAUSTED", "CANCELLED"}
ID = r"([A-Za-z0-9_-]{1,96})"

class Application:
    def __init__(self, db_path, web_root=None, corpus_root=None, qualification_view=False, conversation_profile=False, operations_profile=False):
        from .store import Store
        from .worker import FixtureWorker
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = ProcessLock(str(self.db_path)+".lock")
        try:
            if qualification_view and self.db_path.is_file():
                # A history viewer must never invoke restart recovery on an
                # unfinished CLI run. The CLI uses the same process lock.
                with sqlite3.connect(self.db_path.resolve().as_uri()+"?mode=ro", uri=True) as existing:
                    has_runs = existing.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'").fetchone()
                    if has_runs and existing.execute("SELECT 1 FROM runs WHERE state NOT IN ('COMPLETED','FAILED','TIMED_OUT','BUDGET_EXHAUSTED','CANCELLED') LIMIT 1").fetchone():
                        raise RuntimeError("Qualification history has unfinished work; reconcile the CLI run before opening the viewer")
            self.store = Store(self.db_path)
            self.store.seed_fixture()
            self.qualification_view = qualification_view
            from .incident_history import IncidentHistory
            self.incident_history = IncidentHistory(self.db_path, read_only=qualification_view)
            self.conversation = None
            self.incident_agent = None
            self.incident_workflow = None
            self.memory = None
            self.memory_worker = None
            self.sessions = SessionManager(self.db_path, self.db_path.parent,
                actor_scope="w07-qualified-cli" if qualification_view else "local-owner",
                cookie_name="at_session_qualification" if qualification_view else "at_session_conversation" if conversation_profile else "at_session_operations" if operations_profile else "at_session")
            self.worker = FixtureWorker(self.store)
            self.inspection_worker = None
            corpus_root = Path(corpus_root) if corpus_root is not None else None
            if corpus_root and (corpus_root / "manifest.json").is_file():
                from .adapters.corpus import load_corpus
                from .gateway import Gateway, neutral_incidents
                from .inspection import InspectionWorker
                corpus = load_corpus(corpus_root)
                for incident in neutral_incidents(corpus, system_id="rcaeval_re2ob"):
                    incident["coverage_warnings"].append("Known-onset 60-second development preview; not a held-out or complete-case benchmark.")
                    self.store.register_incident(incident)
                self.inspection_worker = InspectionWorker(self.store, Gateway(self.store, corpus))
            from .live_alerts import LiveAlerts
            from .incident_lifecycle import IncidentLifecycle, LabControl
            control = LabControl.configured() if not qualification_view else None
            self.live_alerts = None if qualification_view else LiveAlerts.configured(self.db_path)
            self.lifecycle = IncidentLifecycle(self.live_alerts, control) if self.live_alerts else None
        except Exception:
            self.lock.close()
            raise
        self.web_root = Path(web_root or ROOT / "apps/web/dist").resolve()

    def close(self):
        if self.incident_workflow:
            self.incident_workflow.close()
        if self.incident_agent:
            self.incident_agent.close()
        if self.live_alerts:
            self.live_alerts.close()
        if self.conversation:
            self.conversation.close()
        self.worker.close()
        if self.inspection_worker:
            self.inspection_worker.close()
        if self.memory_worker:
            self.memory_worker.close()
        if self.memory:
            self.memory.close()
        self.store.close()
        self.lock.close()

    def triage_case(self, case_id):
        from .triage import triage_detail
        if self.live_alerts and case_id.startswith("prom-"):
            case = self.live_alerts.detail(case_id)
            if self.live_alerts.baro:
                ranking = self.live_alerts.baro.view(case_id)
                case["live_baro"] = ranking
                case["evidence"].extend(ranking["evidence"])
            return {"schema_version": VERSION, "case": case}
        return triage_detail(case_id)

class WorkbenchServer(ThreadingHTTPServer):
    daemon_threads = True
    def server_close(self):
        super().server_close()
        if hasattr(self,"app"):
            self.app.close()

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AutoTriagerLocal/0.2"

    def log_message(self, fmt, *args):
        pass  # Do not log questions, cookies, request bodies or query strings.

    def _headers(self, length=None, kind="application/json; charset=utf-8", cookie=None):
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if length is not None:
            self.send_header("Content-Length", str(length))
        if cookie:
            self.send_header("Set-Cookie", self.server.app.sessions.cookie_name+"="+cookie+"; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800")

    def _json(self, status, value):
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self._headers(len(raw))
        self.end_headers()
        self.wfile.write(raw)

    def _error(self, status, code, message, details=None):
        self._json(status, {"schema_version": VERSION,"error": {"code":code,"message":message,"details":details or [],"retryable":False,"correlation_id":"err_"+uuid.uuid4().hex,"run_id":None}})

    def _check_host(self):
        from .errors import APIError
        host = self.headers.get("Host", "")
        if host not in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}:
            raise APIError(403,"ORIGIN_DENIED","Only the configured loopback origin is permitted.")
        return "http://"+host

    def _session(self, mutation=False):
        from .errors import APIError
        origin = self._check_host()
        value = self.server.app.sessions.lookup(self.headers.get("Cookie"))
        if not value:
            raise APIError(401,"UNAUTHENTICATED","Open the local workbench root to establish a session.")
        if mutation:
            if self.headers.get("Origin") != origin:
                raise APIError(403,"ORIGIN_DENIED","The request must come from this workbench origin.")
            if not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), value["csrf_token"]):
                raise APIError(403,"CSRF_FAILED","Refresh the workbench session before retrying.")
        return value

    def _body(self, optional=False):
        from .errors import APIError
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise APIError(400,"INVALID_REQUEST","Invalid request length.")
        if length < 0 or length > 65536 or self.headers.get("Transfer-Encoding"):
            raise APIError(400,"INVALID_REQUEST","Request too large or unsupported framing.")
        if optional and length == 0:
            return None
        if length == 0 or self.headers.get("Content-Type", "").split(";",1)[0] != "application/json":
            raise APIError(400,"INVALID_REQUEST","A JSON object is required.")
        self.connection.settimeout(10)
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError()
            value = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError, TimeoutError):
            raise APIError(400,"INVALID_REQUEST","Invalid JSON request.")
        if not isinstance(value,dict):
            raise APIError(400,"INVALID_REQUEST","A JSON object is required.")
        return value

    def _page(self, query):
        from .errors import APIError
        try:
            if any(len(v)!=1 for v in query.values()):
                raise ValueError()
            limit=int(query.get("limit",["20"])[0])
            if not 1<=limit<=100:
                raise ValueError()
            return {"limit":limit,"cursor":query.get("cursor",[None])[0]}
        except ValueError:
            raise APIError(400,"INVALID_REQUEST","Pagination limit must be between 1 and 100.")

    def _idempotency(self):
        from .errors import APIError
        key=self.headers.get("Idempotency-Key","")
        if not key or len(key)>128:
            raise APIError(400,"INVALID_REQUEST","A bounded Idempotency-Key is required.")
        return key

    def _static(self,path):
        from .errors import APIError
        app=self.server.app
        cookie=None
        if path=="/" and not app.sessions.lookup(self.headers.get("Cookie")):
            cookie,_=app.sessions.issue()
        target=(app.web_root/("index.html" if path=="/" else unquote(path).lstrip("/"))).resolve()
        if not target.is_relative_to(app.web_root) or not target.is_file():
            if path!="/":
                raise APIError(404,"NOT_FOUND","Resource not found.")
            raw=b"<!doctype html><title>AutoTriager setup</title><h1>AutoTriager</h1><p>The backend is running. Build apps/web to open the workbench. Inference is disabled.</p>"
            self.send_response(503)
            self._headers(len(raw),"text/html; charset=utf-8",cookie)
            self.end_headers()
            self.wfile.write(raw)
            return
        raw=target.read_bytes()
        self.send_response(200)
        self._headers(len(raw),mimetypes.guess_type(target.name)[0] or "application/octet-stream",cookie)
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self._route(self._get)

    def do_POST(self):
        self._route(self._post)

    def _route(self,action):
        from .errors import APIError
        try:
            self._check_host()
            action()
        except (BrokenPipeError,ConnectionResetError):
            self.close_connection=True
        except APIError as exc:
            self._error(exc.status,exc.code,exc.message,exc.details)
        except ValidationError as exc:
            self._error(400,"INVALID_REQUEST","Request does not match the contract.",[{"field":".".join(map(str,exc.absolute_path)),"reason":"Schema validation failed."}])
        except (ValueError,TypeError):
            self._error(400,"INVALID_REQUEST","Invalid request parameters.")
        except Exception:
            self._error(500,"INTERNAL_ERROR","The local operation failed. Preserved runs remain available.")

    def _get(self):
        from .errors import APIError
        parsed=urlsplit(self.path)
        path=parsed.path
        query=parse_qs(parsed.query,keep_blank_values=True)
        app=self.server.app
        if path=="/api/v1/health":
            with sqlite3.connect(app.db_path,timeout=10) as db:
                db.execute("SELECT 1").fetchone()
            return self._json(200,validate("Health",{"schema_version":VERSION,"status":"ok","application_status":"ready","store_status":"ready","engine_status":"not_configured"}))
        if not path.startswith("/api/"):
            return self._static(path)
        actor=self._session()["actor"]
        store=app.store
        from .conversation_api import handle_get as conversation_get
        if conversation_get(self,path,query,actor):
            return
        from .knowledge_api import handle_get as knowledge_get
        if knowledge_get(self,path,query,actor):
            return
        if path=="/api/v1/evaluation-summary":
            from .workbench_status import evaluation_summary
            return self._json(200,evaluation_summary())
        if path=="/api/v1/rcaeval-comparison":
            from .workbench_status import rcaeval_comparison
            return self._json(200,rcaeval_comparison())
        if path=="/api/v1/proactive-replay":
            from .proactive_replay import replay_summary
            return self._json(200,replay_summary())
        if path=="/api/v1/monitoring":
            from .monitoring import monitoring_data
            result=monitoring_data()
            if app.live_alerts:
                result["alerts"]=app.live_alerts.summaries()
                result["alert_connection"]=app.live_alerts.status
            return self._json(200,result)
        if path=="/api/v1/incident-history":
            return self._json(200,app.incident_history.list(actor))
        if match:=re.fullmatch("/api/v1/incident-history/"+ID,path):
            return self._json(200,app.incident_history.detail(actor,match[1]))
        if path=="/api/v1/triage":
            from .triage import triage_catalog
            result=triage_catalog()
            if app.live_alerts:
                result["cases"].extend(app.live_alerts.queue())
            return self._json(200,result)
        if match:=re.fullmatch("/api/v1/triage/"+ID,path):
            result=app.triage_case(match[1])
            result["similar"]=app.incident_history.similar(actor,result["case"])
            result["exact"]=app.incident_history.exact_match(actor,result["case"])
            return self._json(200,result)
        if match:=re.fullmatch("/api/v1/baro-evidence/"+ID+"/"+ID,path):
            if match[1].startswith("prom-") and app.live_alerts and app.live_alerts.baro:
                return self._json(200,app.live_alerts.baro.evidence(match[1],match[2]))
            from .triage import baro_evidence
            return self._json(200,baro_evidence(match[1],match[2]))
        if match:=re.fullmatch("/api/v1/incident-metrics/"+ID,path):
            from .incident_metrics import incident_metrics
            return self._json(200,incident_metrics(app,match[1]))
        if match:=re.fullmatch("/api/v1/incident-agent/"+ID,path):
            if query:
                raise APIError(400,"INVALID_REQUEST","The Agent history does not take query parameters.")
            if app.incident_agent is None:
                raise APIError(409,"AGENT_DISABLED","The Incident Agent is not enabled for this workspace.")
            return self._json(200,app.incident_agent.history(actor,match[1]))
        if match:=re.fullmatch("/api/v1/incident-workflow/"+ID,path):
            if app.incident_workflow is None:
                raise APIError(409,"AGENT_DISABLED","The investigation workflow is not enabled.")
            return self._json(200,app.incident_workflow.get(actor,match[1]))
        if app.lifecycle and (match:=re.fullmatch("/api/v1/incident-lifecycle/"+ID,path)):
            return self._json(200,app.lifecycle.describe(match[1]))
        if path=="/api/v1/capabilities":
            value=example("Capabilities")
            value["operations_views"]=True
            value.update(modes=["fixture"] + (["offline_replay"] if app.inspection_worker else []),
                allowed_policy_ids=["synthetic_readonly"] + (["offline_inspection"] if app.inspection_worker else []),
                csrf_token=self._session()["csrf_token"],
                pending_gates=["Real telemetry can be inspected without a model; this is not root-cause diagnosis." if app.inspection_worker else "No audited neutral corpus is installed.",
                    "Official Holmes local-stub compatibility is qualified separately from real provider readiness.",
                    "Browser model execution is not enabled; separately scoped Gemini CLI qualification is documented in the stage handoff."])
            if app.qualification_view:
                value.update(allowed_policy_ids=[],pending_gates=["Read-only view of preserved CLI qualification history. New runs, cancellation and model requests are disabled here."])
            return self._json(200,validate("Capabilities",value))
        if path=="/api/v1/incidents":
            return self._json(200,store.list_incidents(**self._page(query)))
        if path=="/api/v1/investigations":
            return self._json(200,store.list_investigations(actor,**self._page(query)))
        if m:=re.fullmatch("/api/v1/incidents/"+ID,path):
            return self._json(200,store.get_incident(m[1]))
        if m:=re.fullmatch("/api/v1/investigations/"+ID+"/runs",path):
            return self._json(200,store.list_runs(actor,m[1],**self._page(query)))
        if m:=re.fullmatch("/api/v1/investigations/"+ID,path):
            return self._json(200,store.get_investigation(actor,m[1]))
        if m:=re.fullmatch("/api/v1/runs/"+ID+"/evidence/"+ID,path):
            return self._json(200,store.get_evidence(actor,m[1],m[2]))
        if m:=re.fullmatch("/api/v1/runs/"+ID+"/queries/"+ID,path):
            return self._json(200,store.get_query(actor,m[1],m[2]))
        if m:=re.fullmatch("/api/v1/runs/"+ID+"/events",path):
            return self._events(actor,m[1],query)
        if m:=re.fullmatch("/api/v1/runs/"+ID,path):
            return self._json(200,store.get_run(actor,m[1]))
        raise APIError(404,"NOT_FOUND","API operation not found.")

    def _post(self):
        from .errors import APIError
        actor=self._session(mutation=True)["actor"]
        path=urlsplit(self.path).path
        app=self.server.app
        if app.qualification_view:
            raise APIError(403,"QUALIFICATION_READ_ONLY","This profile only displays preserved qualification records.")
        if app.lifecycle and (match:=re.fullmatch("/api/v1/incident-lifecycle/"+ID+"/approve",path)):
            body=self._body()
            if (not isinstance(body,dict) or set(body)!={"plan_id", "approve"} or body["approve"] is not True
                    or not isinstance(body["plan_id"],str) or not re.fullmatch(r"plan-[0-9a-f]{24}",body["plan_id"])):
                raise APIError(400,"INVALID_APPROVAL","Approve the exact proposal shown in the investigation.")
            return self._json(200,app.lifecycle.execute(match[1],body["plan_id"],actor))
        if path=="/api/v1/incident-history":
            body=self._body()
            if (not isinstance(body,dict) or set(body)!={"case_id"} or not isinstance(body["case_id"],str)
                    or not re.fullmatch(ID,body["case_id"])):
                raise APIError(400,"INVALID_REQUEST","Provide one recorded case ID.")
            result=app.incident_history.save(actor,app.triage_case(body["case_id"])["case"])
            return self._json(200 if result["reused"] else 201,result)
        if match:=re.fullmatch("/api/v1/incident-agent/"+ID+"/turns",path):
            if app.incident_agent is None:
                raise APIError(409,"AGENT_DISABLED","The Incident Agent is not enabled for this workspace.")
            return self._json(202,{"turn": app.incident_agent.submit(actor,match[1],self._body(),self._idempotency())})
        if match:=re.fullmatch("/api/v1/incident-workflow/"+ID+"/(continue|verify)",path):
            if app.incident_workflow is None:
                raise APIError(409,"AGENT_DISABLED","The investigation workflow is not enabled.")
            body=self._body()
            if match[2]=="continue":
                return self._json(202,{"turn":app.incident_workflow.submit(actor,match[1],body,self._idempotency())})
            if body!={}:
                raise APIError(400,"INVALID_REQUEST","Recorded recovery verification takes an empty object.")
            return self._json(200,app.incident_workflow.run_check(actor,match[1],None,self._idempotency(),verify=True))
        if match:=re.fullmatch("/api/v1/incident-workflow/"+ID+"/checks/"+ID+"/run",path):
            if app.incident_workflow is None:
                raise APIError(409,"AGENT_DISABLED","The investigation workflow is not enabled.")
            if self._body()!={}:
                raise APIError(400,"INVALID_REQUEST","A server-defined check takes an empty object.")
            return self._json(200,app.incident_workflow.run_check(actor,match[1],match[2],self._idempotency()))
        from .conversation_api import handle_post as conversation_post
        if conversation_post(self,path,actor):
            return
        from .knowledge_api import handle_post as knowledge_post
        if knowledge_post(self,path,actor):
            return
        if path=="/api/v1/investigations":
            body=validate("CreateInvestigation",self._body())
            value,_=app.store.create_investigation(actor,body,self._idempotency())
            return self._json(201,value)
        if m:=re.fullmatch("/api/v1/investigations/"+ID+"/runs",path):
            body=validate("CreateRun",self._body())
            available_modes = {"fixture"} | ({"offline_replay"} if app.inspection_worker else set())
            value,replayed=app.store.create_run(actor,m[1],body,self._idempotency(),available_modes=available_modes)
            if not replayed and value["state"]=="QUEUED":
                if value["mode"] == "fixture":
                    app.worker.submit(value["run_id"])
                elif app.inspection_worker:
                    app.inspection_worker.submit(value["run_id"])
                else:
                    raise APIError(409,"CONFIGURATION_NOT_READY","The actual-data inspection worker is not configured.")
            return self._json(202,value)
        if m:=re.fullmatch("/api/v1/runs/"+ID+"/cancel",path):
            if self._body(optional=True) is not None:
                raise APIError(400,"INVALID_REQUEST","Cancellation takes no body.")
            return self._json(200,app.store.cancel_run(actor,m[1]))
        raise APIError(404,"NOT_FOUND","API operation not found.")

    def _events(self,actor,run_id,query):
        from .errors import APIError
        run=self.server.app.store.get_run(actor,run_id)
        values=query.get("after")
        header=self.headers.get("Last-Event-ID")
        try:
            if values and len(values)!=1:
                raise ValueError()
            raw=values[0] if values else header or "0"
            if not raw.isdigit() or header is not None and (not header.isdigit() or values and int(header)!=int(raw)):
                raise ValueError()
            cursor=int(raw)
            if cursor>run["last_sequence"]:
                raise ValueError()
        except ValueError:
            raise APIError(400,"INVALID_CURSOR","Invalid, conflicting or future event cursor.")
        self.send_response(200)
        self._headers(kind="text/event-stream; charset=utf-8")
        self.send_header("Connection","close")
        self.end_headers()
        self.close_connection=True
        until=time.monotonic()+25
        heartbeat=0.0
        while time.monotonic()<until:
            for event in self.server.app.store.get_events(actor,run_id,after=cursor):
                raw=json.dumps(event,ensure_ascii=False,separators=(",",":"))
                self.wfile.write(f"id: {event['sequence']}\nevent: {event['event_type']}\ndata: {raw}\n\n".encode("utf-8"))
                self.wfile.flush()
                cursor=event["sequence"]
            current=self.server.app.store.get_run(actor,run_id)
            if current["state"] in TERMINAL and cursor>=current["last_sequence"]:
                return
            now=time.monotonic()
            if now-heartbeat>5:
                self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
                heartbeat=now
            time.sleep(.15)

def make_server(host="127.0.0.1",port=0,db_path=None,web_root=None,corpus_root=None,qualification_view=False,conversation_profile=False,operations_profile=False):
    if host!="127.0.0.1":
        raise ValueError("This release binds only to 127.0.0.1")
    server=WorkbenchServer((host,port),Handler)
    try:
        server.app=Application(db_path or ROOT/".local/workbench.sqlite3",web_root,corpus_root,qualification_view,conversation_profile,operations_profile)
        return server
    except Exception:
        server.server_close()
        raise

def main():
    parser=argparse.ArgumentParser(description="AutoTriager local evidence workbench; optional finite grounded conversation")
    parser.add_argument("--port",type=int,default=8830)
    parser.add_argument("--state-dir",type=Path,default=ROOT/".local")
    parser.add_argument("--corpus",type=Path,default=ROOT/"data/analysis")
    parser.add_argument("--operations-profile",action="store_true",help="Use a separate local session cookie for the monitoring and incidents preview")
    parser.add_argument("--qualification-view",action="store_true",help="Read-only view of preserved CLI qualification history; separate local session")
    parser.add_argument("--enable-conversation",action="store_true",help="Enable real Gemini recorded-run chat using the separate persistent 100-attempt/USD2 allowance")
    parser.add_argument("--enable-incident-agent",action="store_true",help="Enable read-only incident chat under the existing shared conversation allowance")
    parser.add_argument("--enable-memory",action="store_true",help="Automatically capture/review source observations into local Knowledge; no model calls")
    parser.add_argument("--enable-memory-rag",action="store_true",help="Use published historical observations in new conversation turns (experimental; requires --enable-memory)")
    parser.add_argument("--seed-public-memory",action="store_true",help="Bootstrap Knowledge with24 source-verified public seed episodes from the fixed reviewed plan")
    args=parser.parse_args()
    if args.enable_conversation and args.qualification_view:
        parser.error("Conversation generation cannot be enabled in a closed qualification history viewer")
    if args.enable_incident_agent and (args.qualification_view or not args.operations_profile):
        parser.error("The Incident Agent requires the local operations profile")
    if args.enable_memory_rag and not args.enable_memory:
        parser.error("Historical RAG requires the reviewed memory store")
    if args.seed_public_memory and not args.enable_memory:
        parser.error("Public memory bootstrap requires --enable-memory")
    if (args.enable_memory or args.enable_memory_rag) and args.qualification_view:
        parser.error("Memory capture cannot mutate a closed qualification viewer")
    if (args.enable_conversation or args.enable_incident_agent or args.enable_memory or args.enable_memory_rag) and args.state_dir.resolve() in {(ROOT/path).resolve() for path in (".local-full-re2ob-r1",".local-full-re2ob-comparison",".local-re2tt-paired-live-20261106")}:
        parser.error("Closed benchmark campaign state cannot be reopened as a conversation app")
    server=make_server(port=args.port,db_path=args.state_dir/"workbench.sqlite3",corpus_root=args.corpus,qualification_view=args.qualification_view,conversation_profile=args.enable_conversation,operations_profile=args.operations_profile)
    if args.enable_memory:
        from .knowledge import MemoryService, MemoryWorker
        from .knowledge_sources import PublicEpisodeSources
        try:
            required = (ROOT/"verification/full-line-packets-20261103-r1/manifest.json", ROOT/"verification/full-line-common-views-20261103/manifest.json", ROOT/"data/full-re2ob-analysis/manifest.json")
            loader = PublicEpisodeSources() if all(path.is_file() for path in required) else None
            server.app.memory = MemoryService(server.app.store,source_loader=loader,auto_publish_observations=True)
            if args.seed_public_memory:
                from .knowledge_sources import fixed_seed_cases
                if loader is None:
                    raise ValueError("Public packet sources are not installed; memory bootstrap is unavailable")
                for case_id in fixed_seed_cases():
                    server.app.memory.import_episode(server.app.sessions.actor_scope,{"source":{"kind":"public_packet","incident_id":case_id}})
            server.app.memory_worker = MemoryWorker(server.app.memory)
        except Exception:
            server.server_close()
            raise
    if args.enable_conversation:
        from .conversation import ConversationService
        from .conversation_provider import GroundedConversationProvider, load_context
        try:
            provider=GroundedConversationProvider.from_environment(args.state_dir)
            enricher = None
            if args.enable_memory_rag:
                from .memory_context import enrich_from_memory
                enricher = lambda actor,context,question: enrich_from_memory(server.app.memory,server.app.store,actor,context,question)
            server.app.conversation=ConversationService(server.app.store,
                lambda actor,inv,run: load_context(server.app.store,actor,inv,run), provider,
                capability_provider=provider.capabilities, model_id=provider.transport.model, workers=1,context_enricher=enricher)
        except Exception:
            server.server_close()
            raise
    if args.enable_incident_agent:
        from .incident_agent import IncidentAgent
        from .incident_agent_provider import IncidentAgentProvider
        try:
            try:
                provider = IncidentAgentProvider.from_environment()
            except (ValueError, RuntimeError, OSError, sqlite3.Error):
                provider = None
            server.app.incident_agent = IncidentAgent(server.app, provider)
            from .incident_workflow import IncidentWorkflow
            server.app.incident_workflow = IncidentWorkflow(server.app)
        except Exception:
            server.server_close()
            raise
    mode="recorded-run Gemini conversation enabled (finite allowance; no actions)" if args.enable_conversation else "read-only inspection | model calls disabled"
    if args.enable_incident_agent:
        mode = "Incident Agent | " + ("bounded Gemini replies" if provider is not None else "model unavailable")
    print(f"AutoTriager: http://127.0.0.1:{server.server_port}/ | {mode} | memory={'on' if args.enable_memory else 'off'} | history retrieval={'on' if args.enable_memory_rag else 'off'}",flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

if __name__=="__main__":
    main()
