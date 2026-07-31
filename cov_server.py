"""Coverage + HSA receipts — gated single-file server for the household.

Serves the coverage explainer page and an HSA receipt tracker. Locked to the
home network (no login on home wifi) and a magic-link token from anywhere else,
so both spouses can reach it but nobody else can.

The receipts matter for decades: the HSA is invested, not spent, and these are
the proof needed to reimburse tax-free years from now. So they are stored in
SQLite with the photos on disk, and there is a CSV export to back them up.

Run:  python3 cov_server.py   ->  http://0.0.0.0:8068
"""
import csv
import io
import os
import sqlite3
import threading
import time
import uuid

import uvicorn
from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response,
    StreamingResponse,
)

VERSION = "1.0.0"
PORT = int(os.environ.get("COV_PORT", "8068"))
HERE = os.path.dirname(os.path.abspath(__file__))
PAGE_PATH = os.path.join(HERE, "index.html")
DATA_DIR = os.environ.get("COV_DATA", os.path.join(HERE, "data"))
PHOTO_DIR = os.path.join(DATA_DIR, "photos")
DB_PATH = os.path.join(DATA_DIR, "receipts.db")

ACCESS_KEY = os.environ.get("COV_ACCESS_KEY", "")
COOKIE = "cov_key"
# The household's public IP(s). On home wifi, requests arrive through Cloudflare
# with this as CF-Connecting-IP, so we let them in with no login.
HOME_IPS = [ip.strip() for ip in os.environ.get(
    "COV_HOME_IPS", "170.203.122.195").split(",") if ip.strip()]
HOME_V6_PREFIX = os.environ.get("COV_HOME_V6", "2606:a300:9008:1bc2:")
OPEN_PREFIXES = ("/receipt-photo/",)  # photos load inside an already-gated page

os.makedirs(PHOTO_DIR, exist_ok=True)

app = FastAPI(title="Coverage")

_lock = threading.Lock()
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.execute("""CREATE TABLE IF NOT EXISTS receipts(
    id TEXT PRIMARY KEY, date TEXT, amount REAL, provider TEXT,
    person TEXT, category TEXT, notes TEXT, photo TEXT,
    reimbursed INTEGER DEFAULT 0, created REAL)""")
_conn.commit()


def client_ip(request: Request) -> str:
    return (request.headers.get("cf-connecting-ip")
            or (request.headers.get("x-forwarded-for", "").split(",")[0].strip())
            or (request.client.host if request.client else ""))


def from_home(request: Request) -> bool:
    ip = client_ip(request)
    return ip in HOME_IPS or (HOME_V6_PREFIX and ip.startswith(HOME_V6_PREFIX))


def _nostore(resp):
    # Never let Cloudflare (or any cache) hold a gated page and serve it to
    # someone who didn't pass the gate. This is a private, per-user site.
    resp.headers["Cache-Control"] = "no-store, private"
    return resp


@app.middleware("http")
async def gate(request: Request, call_next):
    if not ACCESS_KEY:
        return _nostore(await call_next(request))
    path = request.url.path
    if path.startswith(OPEN_PREFIXES) and request.cookies.get(COOKIE) == ACCESS_KEY:
        return _nostore(await call_next(request))
    if from_home(request):
        return _nostore(await call_next(request))
    if path == f"/k/{ACCESS_KEY}":
        resp = Response(status_code=302, headers={"Location": "/"})
        resp.set_cookie(COOKIE, ACCESS_KEY, max_age=60 * 60 * 24 * 365,
                        httponly=True, samesite="lax")
        return _nostore(resp)
    if request.cookies.get(COOKIE) == ACCESS_KEY:
        return _nostore(await call_next(request))
    return _nostore(Response("Not found", status_code=404))


@app.get("/", response_class=HTMLResponse)
def index():
    with open(PAGE_PATH, encoding="utf-8") as fh:
        return fh.read()


@app.get("/api/receipts")
def list_receipts():
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM receipts ORDER BY date DESC, created DESC").fetchall()
    out, unreimb, reimb = [], 0.0, 0.0
    for r in rows:
        out.append({
            "id": r["id"], "date": r["date"], "amount": r["amount"],
            "provider": r["provider"], "person": r["person"],
            "category": r["category"], "notes": r["notes"],
            "has_photo": bool(r["photo"]), "reimbursed": bool(r["reimbursed"]),
        })
        if r["reimbursed"]:
            reimb += r["amount"]
        else:
            unreimb += r["amount"]
    return JSONResponse({"receipts": out, "unreimbursed": round(unreimb, 2),
                         "reimbursed": round(reimb, 2)})


@app.post("/api/receipts")
async def add_receipt(
    date: str = Form(...), amount: float = Form(...), provider: str = Form(...),
    person: str = Form("Family"), category: str = Form("Medical"),
    notes: str = Form(""), photo: UploadFile = File(None),
):
    rid = uuid.uuid4().hex[:12]
    photo_name = ""
    if photo is not None and photo.filename:
        ext = os.path.splitext(photo.filename)[1].lower()[:6] or ".jpg"
        photo_name = f"{rid}{ext}"
        with open(os.path.join(PHOTO_DIR, photo_name), "wb") as fh:
            fh.write(await photo.read())
    with _lock:
        _conn.execute(
            "INSERT INTO receipts(id,date,amount,provider,person,category,notes,photo,created)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (rid, date, float(amount), provider.strip(), person, category,
             notes.strip(), photo_name, time.time()))
        _conn.commit()
    return JSONResponse({"ok": True, "id": rid})


@app.post("/api/receipts/{rid}/reimburse")
def toggle_reimburse(rid: str):
    with _lock:
        row = _conn.execute("SELECT reimbursed FROM receipts WHERE id=?", (rid,)).fetchone()
        if not row:
            return JSONResponse({"error": "not found"}, status_code=404)
        _conn.execute("UPDATE receipts SET reimbursed=? WHERE id=?",
                      (0 if row["reimbursed"] else 1, rid))
        _conn.commit()
    return JSONResponse({"ok": True})


@app.delete("/api/receipts/{rid}")
def delete_receipt(rid: str):
    with _lock:
        row = _conn.execute("SELECT photo FROM receipts WHERE id=?", (rid,)).fetchone()
        if row and row["photo"]:
            try:
                os.remove(os.path.join(PHOTO_DIR, row["photo"]))
            except OSError:
                pass
        _conn.execute("DELETE FROM receipts WHERE id=?", (rid,))
        _conn.commit()
    return JSONResponse({"ok": True})


@app.get("/receipt-photo/{rid}")
def receipt_photo(rid: str):
    with _lock:
        row = _conn.execute("SELECT photo FROM receipts WHERE id=?", (rid,)).fetchone()
    if not row or not row["photo"]:
        return Response("no photo", status_code=404)
    path = os.path.join(PHOTO_DIR, row["photo"])
    if not os.path.exists(path):
        return Response("gone", status_code=404)
    return FileResponse(path)


@app.get("/api/receipts/export.csv")
def export_csv():
    with _lock:
        rows = _conn.execute(
            "SELECT date,amount,provider,person,category,notes,reimbursed"
            " FROM receipts ORDER BY date").fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Date", "Amount", "For", "Who", "Type", "Notes", "Reimbursed"])
    for r in rows:
        w.writerow([r["date"], f'{r["amount"]:.2f}', r["provider"], r["person"],
                    r["category"], r["notes"], "yes" if r["reimbursed"] else "no"])
    return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                             headers={"Content-Disposition": 'attachment; filename="hsa-receipts.csv"'})


if __name__ == "__main__":
    print(f"Coverage v{VERSION} -> http://0.0.0.0:{PORT}  data={DATA_DIR}")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
