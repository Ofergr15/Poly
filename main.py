#!/usr/bin/env python3
"""
Polymarket Trading Dashboard - Public Server
FastAPI app with Google OAuth, Supabase, and admin access control.
"""

import os
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from authlib.integrations.starlette_client import OAuth
from starlette.middleware.sessions import SessionMiddleware
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

# ─── Config ───────────────────────────────────────────────────────────────────

SECRET_KEY           = os.environ.get("SECRET_KEY", "dev-secret-change-me")
GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
SIMULATION_API       = os.environ.get("SIMULATION_API", "http://localhost:8889")
PUBLIC_URL           = os.environ.get("PUBLIC_URL", "").rstrip("/")
SUPABASE_URL         = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY         = os.environ.get("SUPABASE_KEY", "")
ADMIN_EMAIL          = os.environ.get("ADMIN_EMAIL", "grosfeldofer@gmail.com")

TEMPLATES_DIR = Path(__file__).parent / "templates"

# ─── App ──────────────────────────────────────────────────────────────────────

app = FastAPI(title="Polymarket Trading Dashboard")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, max_age=86400 * 7)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

oauth = OAuth()
oauth.register(
    name="google",
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

# ─── Supabase ─────────────────────────────────────────────────────────────────

_sb = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None


def check_access(email: str) -> str:
    """Returns 'approved', 'pending', 'denied', or 'unknown'."""
    if not _sb:
        return "approved"
    try:
        res = _sb.table("access_requests").select("status").eq("email", email).limit(1).execute()
        return res.data[0]["status"] if res.data else "unknown"
    except Exception:
        return "unknown"


def request_access(email: str, name: str, picture: str):
    """Create a pending access request."""
    if not _sb:
        return
    try:
        _sb.table("access_requests").upsert(
            {"email": email, "name": name, "picture": picture, "status": "pending"},
            on_conflict="email"
        ).execute()
    except Exception as e:
        print(f"[DB] request_access error: {e}")


def get_all_requests() -> list:
    if not _sb:
        return []
    try:
        res = _sb.table("access_requests").select("*").order("requested_at", desc=True).execute()
        return res.data or []
    except Exception:
        return []


def set_request_status(email: str, status: str):
    if not _sb:
        return
    from datetime import datetime, timezone
    try:
        _sb.table("access_requests").update({
            "status": status,
            "reviewed_at": datetime.now(timezone.utc).isoformat()
        }).eq("email", email).execute()
    except Exception as e:
        print(f"[DB] set_request_status error: {e}")


def save_trades(trades: list):
    if not trades or not _sb:
        return
    try:
        res = _sb.table("trades").select("win_window").order("win_window", desc=True).limit(1).execute()
        max_win = res.data[0]["win_window"] if res.data else 0
        new = [t for t in trades if (t.get("window") or 0) > max_win]
        if new:
            rows = [{**t, "win_window": t.pop("window", None)} for t in new]
            _sb.table("trades").upsert(rows, on_conflict="win_window").execute()
    except Exception as e:
        print(f"[DB] save_trades error: {e}")


def save_stats(stats: dict):
    if not _sb:
        return
    try:
        _sb.table("stats_snapshots").insert({
            "total_trades":    stats.get("total_trades", 0),
            "wins":            stats.get("wins", 0),
            "losses":          stats.get("losses", 0),
            "win_rate":        stats.get("win_rate", 0),
            "total_pnl":       stats.get("total_pnl", 0),
            "elapsed_hours":   stats.get("elapsed_hours", 0),
            "trades_per_hour": stats.get("trades_per_hour", 0),
            "rejected_trades": stats.get("rejected_trades", 0),
            "current_window":  stats.get("current_window", 0),
        }).execute()
    except Exception as e:
        print(f"[DB] save_stats error: {e}")


def load_latest_stats() -> Optional[dict]:
    if not _sb:
        return None
    try:
        res = _sb.table("stats_snapshots").select("*").order("id", desc=True).limit(1).execute()
        return res.data[0] if res.data else None
    except Exception:
        return None


def load_trades_from_db() -> list:
    if not _sb:
        return []
    try:
        res = _sb.table("trades").select(
            "win_window,timestamp,side,entry_price,exit_price,gross_pnl,fee,slippage,pnl,result,confidence"
        ).order("win_window", desc=False).execute()
        return res.data or []
    except Exception:
        return []

# ─── Auth helpers ─────────────────────────────────────────────────────────────

def get_user(request: Request) -> Optional[dict]:
    return request.session.get("user")

def is_admin(user: dict) -> bool:
    return user.get("email") == ADMIN_EMAIL

# ─── Auth routes ──────────────────────────────────────────────────────────────

@app.get("/login", include_in_schema=False)
async def login(request: Request):
    redirect_uri = f"{PUBLIC_URL}/auth/callback" if PUBLIC_URL else str(request.url_for("auth_callback"))
    return await oauth.google.authorize_redirect(request, redirect_uri)


@app.get("/auth/callback", name="auth_callback", include_in_schema=False)
async def auth_callback(request: Request):
    token = await oauth.google.authorize_access_token(request)
    user_info = token.get("userinfo") or {}
    email   = user_info.get("email", "")
    name    = user_info.get("name", email)
    picture = user_info.get("picture", "")

    status = check_access(email)

    if status == "approved":
        request.session["user"] = {"email": email, "name": name, "picture": picture}
        return RedirectResponse("/", status_code=302)

    if status == "denied":
        return templates.TemplateResponse("request_access.html", {
            "request": request, "email": email, "name": name, "picture": picture,
            "state": "denied"
        })

    if status == "pending":
        return templates.TemplateResponse("request_access.html", {
            "request": request, "email": email, "name": name, "picture": picture,
            "state": "pending"
        })

    # Unknown — show request access form
    return templates.TemplateResponse("request_access.html", {
        "request": request, "email": email, "name": name, "picture": picture,
        "state": "new"
    })


@app.post("/request-access", include_in_schema=False)
async def submit_access_request(request: Request):
    form = await request.form()
    email   = form.get("email", "")
    name    = form.get("name", "")
    picture = form.get("picture", "")
    if email:
        request_access(email, name, picture)
    return templates.TemplateResponse("request_access.html", {
        "request": request, "email": email, "name": name, "picture": picture,
        "state": "submitted"
    })


@app.get("/logout", include_in_schema=False)
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login-page", status_code=302)

# ─── Page routes ──────────────────────────────────────────────────────────────

@app.get("/login-page", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request):
    user = get_user(request)
    if user and check_access(user["email"]) == "approved":
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request})


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    user = get_user(request)
    if not user:
        return RedirectResponse("/login-page", status_code=302)
    if check_access(user["email"]) != "approved":
        return RedirectResponse("/login-page", status_code=302)
    return templates.TemplateResponse("dashboard.html", {"request": request, "user": user, "is_admin": is_admin(user)})


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    user = get_user(request)
    if not user or not is_admin(user):
        return RedirectResponse("/", status_code=302)
    requests_list = get_all_requests()
    pending = [r for r in requests_list if r["status"] == "pending"]
    approved = [r for r in requests_list if r["status"] == "approved"]
    denied = [r for r in requests_list if r["status"] == "denied"]
    return templates.TemplateResponse("admin.html", {
        "request": request, "user": user,
        "pending": pending, "approved": approved, "denied": denied,
    })


@app.post("/admin/approve", include_in_schema=False)
async def admin_approve(request: Request):
    user = get_user(request)
    if not user or not is_admin(user):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    form = await request.form()
    email = form.get("email", "")
    set_request_status(email, "approved")
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/deny", include_in_schema=False)
async def admin_deny(request: Request):
    user = get_user(request)
    if not user or not is_admin(user):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    form = await request.form()
    email = form.get("email", "")
    set_request_status(email, "denied")
    return RedirectResponse("/admin", status_code=302)

# ─── API routes ───────────────────────────────────────────────────────────────

def _auth_check(request: Request):
    user = get_user(request)
    if not user or check_access(user["email"]) != "approved":
        return None, JSONResponse({"error": "Not authenticated"}, status_code=401)
    return user, None


@app.get("/api/week1_stats")
async def api_week1_stats(request: Request):
    user, err = _auth_check(request)
    if err:
        return err
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{SIMULATION_API}/api/week1_stats")
            stats = resp.json()
        save_stats(stats)
        return JSONResponse(stats)
    except Exception:
        stats = load_latest_stats()
        if stats:
            return JSONResponse(stats)
        return JSONResponse({
            "total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0,
            "total_pnl": 0, "elapsed_hours": 0, "trades_per_hour": 0,
            "rejected_trades": 0, "current_window": 0, "_offline": True,
        })


@app.get("/api/week1_trades")
async def api_week1_trades(request: Request):
    user, err = _auth_check(request)
    if err:
        return err
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{SIMULATION_API}/api/week1_trades")
            data = resp.json()
        save_trades(data.get("all_trades", []))
        return JSONResponse(data)
    except Exception:
        trades = load_trades_from_db()
        return JSONResponse({"all_trades": trades, "rejected_trades": [], "_offline": True})


@app.get("/api/me")
async def api_me(request: Request):
    user = get_user(request)
    if not user:
        return JSONResponse({"authenticated": False}, status_code=401)
    return JSONResponse({"authenticated": True, **user})


@app.get("/api/admin/requests")
async def api_admin_requests(request: Request):
    user = get_user(request)
    if not user or not is_admin(user):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    return JSONResponse(get_all_requests())

# ─── Startup ──────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    print(f"✓ Supabase: {'connected' if _sb else 'NOT connected'}")
    print(f"✓ Admin: {ADMIN_EMAIL}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=False)
