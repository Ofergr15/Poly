#!/usr/bin/env python3
"""
Polymarket Trading Dashboard - Public Server
FastAPI app with Google OAuth, Supabase persistence, and live simulation proxy.
"""

import os
import json
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

SECRET_KEY       = os.environ.get("SECRET_KEY", "dev-secret-change-me")
GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
SIMULATION_API   = os.environ.get("SIMULATION_API", "http://localhost:8889")
PUBLIC_URL       = os.environ.get("PUBLIC_URL", "").rstrip("/")
SUPABASE_URL     = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY     = os.environ.get("SUPABASE_KEY", "")

_raw_allowed = os.environ.get("ALLOWED_EMAILS", "").strip()
ALLOWED_EMAILS: Optional[set] = set(e.strip() for e in _raw_allowed.split(",") if e.strip()) or None

TEMPLATES_DIR = Path(__file__).parent / "templates"

# ─── App setup ────────────────────────────────────────────────────────────────

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


def save_trades(trades: list):
    if not trades or not _sb:
        return
    try:
        # Get max window already in DB to avoid re-inserting
        res = _sb.table("trades").select("window").order("window", desc=True).limit(1).execute()
        max_win = res.data[0]["window"] if res.data else 0
        new = [t for t in trades if (t.get("window") or 0) > max_win]
        if new:
            _sb.table("trades").upsert(new, on_conflict="window").execute()
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
    except Exception as e:
        print(f"[DB] load_latest_stats error: {e}")
        return None


def load_trades_from_db() -> list:
    if not _sb:
        return []
    try:
        res = _sb.table("trades").select(
            "window,timestamp,side,entry_price,exit_price,gross_pnl,fee,slippage,pnl,result,confidence"
        ).order("window", desc=False).execute()
        return res.data or []
    except Exception as e:
        print(f"[DB] load_trades error: {e}")
        return []

# ─── Auth helpers ─────────────────────────────────────────────────────────────

def get_user(request: Request) -> Optional[dict]:
    return request.session.get("user")


def is_allowed(user: dict) -> bool:
    if ALLOWED_EMAILS is None:
        return True
    return user.get("email") in ALLOWED_EMAILS

# ─── Auth routes ──────────────────────────────────────────────────────────────

@app.get("/login", include_in_schema=False)
async def login(request: Request):
    if PUBLIC_URL:
        redirect_uri = f"{PUBLIC_URL}/auth/callback"
    else:
        redirect_uri = str(request.url_for("auth_callback"))
    return await oauth.google.authorize_redirect(request, redirect_uri)


@app.get("/auth/callback", name="auth_callback", include_in_schema=False)
async def auth_callback(request: Request):
    token = await oauth.google.authorize_access_token(request)
    user_info = token.get("userinfo") or {}
    email = user_info.get("email", "")

    if ALLOWED_EMAILS and email not in ALLOWED_EMAILS:
        return HTMLResponse(
            f"<h2>Access denied</h2><p>{email} is not on the allowed list.</p>"
            "<p><a href='/login-page'>Back to login</a></p>",
            status_code=403,
        )

    request.session["user"] = {
        "email": email,
        "name": user_info.get("name", email),
        "picture": user_info.get("picture", ""),
    }
    return RedirectResponse("/", status_code=302)


@app.get("/logout", include_in_schema=False)
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login-page", status_code=302)

# ─── Page routes ──────────────────────────────────────────────────────────────

@app.get("/login-page", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request):
    user = get_user(request)
    if user and is_allowed(user):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request})


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    user = get_user(request)
    if not user:
        return RedirectResponse("/login-page", status_code=302)
    if not is_allowed(user):
        return RedirectResponse("/login-page", status_code=302)
    return templates.TemplateResponse("dashboard.html", {"request": request, "user": user})

# ─── API routes ───────────────────────────────────────────────────────────────

def _auth_check(request: Request):
    """Returns (user, error_response). If error_response is set, return it early."""
    user = get_user(request)
    if not user or not is_allowed(user):
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
        # Simulation offline — serve last known state from DB
        stats = load_latest_stats()
        if stats:
            return JSONResponse(stats)
        return JSONResponse({
            "total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0,
            "total_pnl": 0, "elapsed_hours": 0, "trades_per_hour": 0,
            "rejected_trades": 0, "current_window": 0,
            "_offline": True,
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
        trades = data.get("all_trades", [])
        save_trades(trades)
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

# ─── Startup ──────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    if _sb:
        print(f"✓ Supabase connected: {SUPABASE_URL}")
    else:
        print("⚠️  SUPABASE_URL/KEY not set — DB fallback disabled")
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        print("⚠️  GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET not set — OAuth will not work")
    if ALLOWED_EMAILS:
        print(f"✓ Access restricted to: {', '.join(ALLOWED_EMAILS)}")
    else:
        print("⚠️  ALLOWED_EMAILS not set — any Google account can log in")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=False)
