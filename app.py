"""Loopback-only local web application."""
from __future__ import annotations
import argparse
import hmac
import json
import mimetypes
import secrets
import threading
import urllib.parse
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from core import Store, Tutor, COURSES, TZ, compact, contained
from study import StudyFlow

ROOT=Path(__file__).resolve().parent


def serve(root=ROOT,port=None):
    store=Store(root)
    tutor=Tutor(store)
    study=StudyFlow(store,tutor)
    port=port or int(store.settings.get("port",18765))
    tokenfile=store.data/"local-token.txt"
    if not tokenfile.exists(): tokenfile.write_text(secrets.token_urlsafe(32),encoding="utf-8")
    token=tokenfile.read_text(encoding="utf-8").strip()
    origin=f"http://127.0.0.1:{port}"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self,fmt,*args):
            # No questions, note contents, or authentication tokens in logs.
            return
        def send_headers(self,status=200,kind="application/json; charset=utf-8",cookie=False,length=0):
            self.send_response(status)
            self.send_header("Content-Type",kind)
            self.send_header("Content-Length",str(length))
            self.send_header("Connection","close")
            self.send_header("Cache-Control","no-store")
            self.send_header("X-Content-Type-Options","nosniff")
            self.send_header("Referrer-Policy","no-referrer")
            self.send_header("Content-Security-Policy","default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            if cookie: self.send_header("Set-Cookie",f"study_session={token}; HttpOnly; SameSite=Strict; Path=/")
            self.end_headers()
        def output(self,data,status=200):
            body=compact(data).encode("utf-8")
            self.send_headers(status,length=len(body))
            try: self.wfile.write(body)
            except (BrokenPipeError,ConnectionResetError): pass
        def host_ok(self):
            return self.headers_in().get("Host","") in {f"127.0.0.1:{port}",f"localhost:{port}"}
        def headers_in(self):
            return self._incoming
        def authorized(self,write=False):
            hdr=self.headers_in()
            auth=hdr.get("Authorization","")
            if auth.startswith("Bearer ") and hmac.compare_digest(auth[7:],token): return True
            try:
                cookie=SimpleCookie(hdr.get("Cookie",""))
                value=cookie.get("study_session")
                if not value or not hmac.compare_digest(value.value,token): return False
            except Exception: return False
            source=hdr.get("Origin")
            if source and source not in {origin,f"http://localhost:{port}"}: return False
            if hdr.get("Sec-Fetch-Site") in {"cross-site","same-site"}: return False
            if write and (not source or hdr.get("X-Study-Request")!="1"): return False
            return True
        def setup_input(self):
            # Keep the parsed incoming headers separate from response generation.
            self._incoming=self.headers
            self.connection.settimeout(15)
            return self.host_ok()
        def do_GET(self):
            if not self.setup_input(): return self.output({"error":"仅允许本机访问。"},403)
            path=urllib.parse.urlsplit(self.path)
            args={k:v[0] for k,v in urllib.parse.parse_qs(path.query).items()}
            if path.path=="/health": return self.output({"ok":True,"app":"孙权","version":"0.3.0"})
            assets={"/":"index.html","/index.html":"index.html","/style.css":"style.css","/app.js":"app.js",
                    "/sunquan-icon.png":"sunquan-icon.png","/favicon.ico":"sunquan.ico"}
            if path.path in assets:
                file=root/"web"/assets[path.path]
                if not file.is_file(): return self.output({"error":"界面文件尚未就绪。"},503)
                mime=mimetypes.guess_type(str(file))[0] or "text/plain"
                body=file.read_bytes()
                content_type=mime+"; charset=utf-8" if mime.startswith("text/") or file.suffix==".js" else mime
                self.send_headers(kind=content_type,cookie=path.path in {"/","/index.html"},length=len(body))
                self.wfile.write(body); return
            if not self.authorized(): return self.output({"error":"请先打开本地学习界面。"},401)
            try:
                if path.path=="/api/study/plan":
                    if args.get("easy","0") not in {"0","1"}: raise ValueError("轻松任务选项无效。")
                    return self.output(study.plan(args.get("course",""),args.get("minutes","5"),args.get("easy")=="1"))
                if path.path=="/api/study/run": return self.output({"run":study.get(args.get("id",""))})
                if path.path=="/api/state":
                    return self.output({"app":"孙权","date":datetime.now(TZ).strftime("%Y年%m月%d日"),
                                        "model":tutor.status(),"courses":[{"id":c,"title":c} for c in COURSES],
                                        "snapshot":store.snapshot(),"reminder":store.reminder(),"stats":store.stats(),"source_root":str(store.source)})
                if path.path=="/api/memory":
                    course=args.get("course","")
                    snap=store.snapshot(course)
                    return self.output({"revision":snap["revision"],"items":store.memories(course),"handoff":snap["handoff"]})
                if path.path=="/api/handoff": return self.output(store.snapshot(args.get("course",""),args.get("since_revision")))
                if path.path=="/api/sources": return self.output({"items":store.search(args.get("q",""),args.get("course",""),30)})
                if path.path=="/api/source": return self.output(store.read_source(args.get("id","")))
                if path.path=="/api/history": return self.output({"items":store.history(args.get("session_id","")),"session_id":args.get("session_id","")})
                if path.path=="/api/reminder": return self.output(store.reminder())
                if path.path=="/api/model": return self.output(tutor.status())
                if path.path=="/api/coverage":
                    report=json.loads((store.data/"coverage.json").read_text(encoding="utf-8"))
                    return self.output(report)
                if path.path=="/api/preview":
                    info=store.read_source(args.get("id",""))
                    file=store.source/info.get("preview","")
                    allowed=store.source/"工具/课件搜索/generated/previews"
                    if not contained(file,allowed) or not file.is_file() or file.suffix not in {".jpg",".png"}:
                        return self.output({"error":"此页没有原图预览。"},404)
                    body=file.read_bytes()
                    self.send_headers(kind="image/jpeg" if file.suffix==".jpg" else "image/png",length=len(body))
                    self.wfile.write(body);return
                return self.output({"error":"没有这个接口。"},404)
            except (ValueError,OSError) as exc: return self.output({"error":str(exc)[:300]},400)
        def do_POST(self):
            # Consume a bounded body before rejecting a request; closing a Windows
            # socket with unread input can reset it before the error response arrives.
            try:
                size=int(self.headers.get("Content-Length","0"))
            except ValueError:
                size=0
            self.connection.settimeout(15)
            raw=self.rfile.read(size) if 0<size<=100000 else b""
            if not self.setup_input(): return self.output({"error":"仅允许本机访问。"},403)
            if not self.authorized(write=True): return self.output({"error":"请求来源未通过校验。"},403)
            if self.headers_in().get("Content-Type","").split(";")[0]!="application/json":
                return self.output({"error":"请求格式错误。"},415)
            try:
                if size<=0 or size>100000: raise ValueError("请求大小超出范围。")
                data=json.loads(raw)
                if not isinstance(data,dict): raise ValueError("请求必须是对象。")
                path=urllib.parse.urlsplit(self.path).path
                if path=="/api/study/start":
                    return self.output(study.start(data.get("course") or "",data.get("minutes",5),data.get("easy",False),data.get("idempotency_key")))
                if path=="/api/study/check": return self.output({"run":study.check(data.get("id",""))})
                if path=="/api/study/answer": return self.output({"run":study.answer(data.get("id",""),data.get("answers"),data.get("idempotency_key"))})
                if path=="/api/study/feedback": return self.output({"run":study.feedback(data.get("id",""))})
                if path=="/api/study/finish":
                    return self.output({"run":study.finish(data.get("id",""),data.get("stuck_point",""),
                        skip_quiz=data.get("skip_quiz",False),stop_note=data.get("stop_note",""))})
                if path=="/api/chat":
                    if data.get("study_run_id"):
                        run=study.get(data["study_run_id"])
                        if (data.get("course") or "综合")!=run['course'] or data.get("session_id")!=run['chat_session_id']:
                            raise ValueError("这次小步与当前课程或对话不匹配，请重新接续。")
                        context=compact({k:run[k] for k in ('course','minutes','title','context','previous_question','steps','sources')})
                        return self.output(tutor.answer(data.get("message",""),run['course'],run['chat_session_id'],bool(data.get("web")),data.get("url",""),study_context=context))
                    return self.output(tutor.answer(data.get("message",""),data.get("course") or "综合",data.get("session_id"),bool(data.get("web")),data.get("url","")))
                if path=="/api/memory":
                    return self.output(store.remember(data.get("content",""),data.get("course") or "综合",data.get("kind","user_reported"),
                        idempotency_key=data.get("idempotency_key"),expected_revision=data.get("expected_revision"),supersedes=data.get("supersedes")))
                if path=="/api/memory/delete": return self.output(store.forget(data.get("id","")))
                if path=="/api/reminder": return self.output(store.reminder(data.get("action")))
                if path=="/api/reindex": return self.output(store.index())
                if path=="/api/unload":
                    tutor.rpc("/api/generate",{"model":tutor.model,"keep_alive":0},timeout=30)
                    return self.output({"ok":True})
                if path=="/api/shutdown":
                    self.output({"ok":True})
                    threading.Thread(target=server.shutdown,daemon=True).start();return
                return self.output({"error":"没有这个接口。"},404)
            except (ValueError,OSError,TypeError) as exc: return self.output({"error":str(exc)[:300]},400)
            except Exception:
                return self.output({"error":"处理失败，已有记录已保留，请重试。"},500)
    server=ThreadingHTTPServer(("127.0.0.1",port),Handler)
    server.daemon_threads=True
    try: store.index()
    except (OSError,ValueError):
        if not (store.data/"coverage.json").exists(): raise
    try: server.serve_forever(poll_interval=.5)
    finally:
        server.server_close()
        if tutor.process and tutor.process.poll() is None:
            tutor.process.terminate()


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--index",action="store_true")
    parser.add_argument("--port",type=int)
    parser.add_argument("--root",type=Path,default=ROOT)
    args=parser.parse_args()
    if args.index: print(compact(Store(args.root).index()))
    else: serve(args.root,args.port)
