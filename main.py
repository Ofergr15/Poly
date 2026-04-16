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
LIVE_SERVER_URL      = os.environ.get("LIVE_SERVER_URL", "").rstrip("/")   # e.g. http://your-vps-ip:8891
LIVE_SERVER_SECRET   = os.environ.get("LIVE_SERVER_SECRET", "")

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


def compute_stats_from_db(source: str = "demo", since: str = None) -> Optional[dict]:
    """Calculate comprehensive stats directly from Supabase trades table.

    Args:
        since: ISO timestamp string — only include trades at or after this time.
    """
    if not _sb:
        return None
    try:
        q = _sb.table("trades").select(
            "pnl,gross_pnl,fee,slippage,win_window,synced_at,confidence,side"
        ).eq("source", source)
        if since:
            q = q.gte("synced_at", since)
        res = q.order("win_window", desc=False).execute()
        trades = res.data or []
        if not trades:
            return {"total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0,
                    "total_pnl": 0, "elapsed_hours": 0, "trades_per_hour": 0,
                    "rejected_trades": 0, "current_window": 0,
                    "cum_pnl_series": [], "hourly_pnl": {},
                    "profit_factor": None, "max_drawdown": 0,
                    "sharpe_ratio": 0}

        total      = len(trades)
        pnls       = [t.get("pnl") or 0 for t in trades]
        gross_pnls = [t.get("gross_pnl") or 0 for t in trades]
        fees       = [t.get("fee") or 0 for t in trades]
        slippages  = [t.get("slippage") or 0 for t in trades]
        confs      = [t.get("confidence") or 0 for t in trades]

        wins   = sum(1 for p in pnls if p > 0)
        losses = total - wins

        # Elapsed time
        from datetime import datetime, timezone
        import math
        try:
            first   = datetime.fromisoformat(trades[0]["synced_at"].replace("Z", "+00:00"))
            last    = datetime.fromisoformat(trades[-1]["synced_at"].replace("Z", "+00:00"))
            elapsed = max((last - first).total_seconds() / 3600, 0.01)
        except Exception:
            elapsed = 1.0

        # Cumulative P&L & drawdown (with timestamps for chart tooltips)
        cum, peak, max_dd = 0, 0, 0
        cum_pnls = []
        for i, (p, t) in enumerate(zip(pnls, trades)):
            cum += p
            try:
                dt = datetime.fromisoformat(t["synced_at"].replace("Z", "+00:00"))
                ts = dt.strftime("%Y-%m-%dT%H:%M:%SZ")  # UTC ISO — browser converts to local
            except Exception:
                ts = str(i)
            cum_pnls.append({"t": ts, "v": round(cum, 4)})
            if cum > peak:
                peak = cum
            dd = peak - cum
            if dd > max_dd:
                max_dd = dd

        # Streaks
        cur_streak, max_win_streak, max_loss_streak = 0, 0, 0
        streak_type = None
        cur_w, cur_l = 0, 0
        for p in pnls:
            if p > 0:
                cur_w += 1; cur_l = 0
                max_win_streak = max(max_win_streak, cur_w)
                cur_streak = cur_w; streak_type = "win"
            else:
                cur_l += 1; cur_w = 0
                max_loss_streak = max(max_loss_streak, cur_l)
                cur_streak = cur_l; streak_type = "loss"

        # Profit factor
        gross_wins   = sum(p for p in pnls if p > 0)
        gross_losses = abs(sum(p for p in pnls if p <= 0))
        profit_factor = round(gross_wins / gross_losses, 2) if gross_losses > 0 else 999

        # Sharpe (simplified daily)
        import statistics
        sharpe = 0
        if len(pnls) > 1:
            try:
                avg_r = statistics.mean(pnls)
                std_r = statistics.stdev(pnls)
                sharpe = round((avg_r / std_r) * math.sqrt(252) if std_r > 0 else 0, 2)
            except Exception:
                sharpe = 0

        # Hourly P&L buckets
        hourly = {}
        for t in trades:
            try:
                dt  = datetime.fromisoformat(t["synced_at"].replace("Z", "+00:00"))
                key = dt.strftime("%H:00")
                hourly[key] = round(hourly.get(key, 0) + (t.get("pnl") or 0), 4)
            except Exception:
                pass

        # Side breakdown
        buy_up   = [t for t in trades if t.get("side") == "BUY_UP"]
        buy_down = [t for t in trades if t.get("side") == "BUY_DOWN"]

        total_pnl = sum(pnls)

        return {
            "total_trades":      total,
            "wins":              wins,
            "losses":            losses,
            "win_rate":          round(wins / total * 100, 1),
            "total_pnl":         round(total_pnl, 4),
            "elapsed_hours":     round(elapsed, 2),
            "trades_per_hour":   round(total / elapsed, 1),
            "rejected_trades":   0,
            "current_window":    trades[-1].get("win_window", 0),
            # Extended stats
            "avg_pnl":           round(total_pnl / total, 4),
            "best_trade":        round(max(pnls), 4),
            "worst_trade":       round(min(pnls), 4),
            "max_drawdown":      round(max_dd, 4),
            "total_fees":        round(sum(fees), 4),
            "total_slippage":    round(sum(slippages), 4),
            "total_gross_pnl":   round(sum(gross_pnls), 4),
            "profit_factor":     profit_factor,
            "sharpe_ratio":      sharpe,
            "max_win_streak":    max_win_streak,
            "max_loss_streak":   max_loss_streak,
            "current_streak":    cur_streak,
            "current_streak_type": streak_type,
            "avg_confidence":    round(sum(confs) / len(confs), 1) if confs else 0,
            "buy_up_count":      len(buy_up),
            "buy_down_count":    len(buy_down),
            "buy_up_wr":         round(sum(1 for t in buy_up if (t.get("pnl") or 0) > 0) / len(buy_up) * 100, 1) if buy_up else 0,
            "buy_down_wr":       round(sum(1 for t in buy_down if (t.get("pnl") or 0) > 0) / len(buy_down) * 100, 1) if buy_down else 0,
            "hourly_pnl":        hourly,
            "cum_pnl_series":    cum_pnls,
        }
    except Exception as e:
        print(f"[DB] compute_stats error: {e}")
        return None


def save_trades(trades: list):
    if not trades or not _sb:
        return
    try:
        res = _sb.table("trades").select("win_window").order("win_window", desc=True).limit(1).execute()
        max_win = res.data[0]["win_window"] if res.data else 0
        new = [t for t in trades if (t.get("window") or 0) > max_win]
        if new:
            rows = [{**t, "win_window": t.pop("window", None), "source": "demo"} for t in new]
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


def load_trades_from_db(source: str = "demo", since: str = None) -> list:
    if not _sb:
        return []
    try:
        q = _sb.table("trades").select(
            "win_window,timestamp,side,entry_price,exit_price,gross_pnl,fee,slippage,pnl,result,confidence,synced_at"
        ).eq("source", source)
        if since:
            q = q.gte("synced_at", since)
        res = q.order("win_window", desc=False).execute()
        return res.data or []
    except Exception:
        return []

# ─── Auth helpers ─────────────────────────────────────────────────────────────

def get_user(request: Request) -> Optional[dict]:
    return request.session.get("user")

def is_admin(user: dict) -> bool:
    return user.get("email") == ADMIN_EMAIL

# ─── Auth routes ──────────────────────────────────────────────────────────────

@app.get("/debug-redirect", include_in_schema=False)
async def debug_redirect(request: Request):
    redirect_uri = f"{PUBLIC_URL}/auth/callback" if PUBLIC_URL else str(request.url_for("auth_callback"))
    return JSONResponse({"redirect_uri": redirect_uri, "public_url": PUBLIC_URL, "client_id": GOOGLE_CLIENT_ID[:20] + "..."})

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


@app.get("/api/demo/session")
async def api_demo_session(request: Request):
    """Return ISO timestamp when the demo bot last started (proxied from VPS)."""
    user, err = _auth_check(request)
    if err:
        return err
    data = await _proxy("demo/session")
    return JSONResponse({"started_at": data.get("started_at")})


@app.get("/api/week1_stats")
async def api_week1_stats(request: Request, since: str = None):
    user, err = _auth_check(request)
    if err:
        return err
    stats = compute_stats_from_db("demo", since=since)
    if stats:
        return JSONResponse(stats)
    return JSONResponse({
        "total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0,
        "total_pnl": 0, "elapsed_hours": 0, "trades_per_hour": 0,
        "rejected_trades": 0, "current_window": 0,
        "cum_pnl_series": [], "profit_factor": None, "max_drawdown": 0,
    })


@app.get("/api/week1_trades")
async def api_week1_trades(request: Request, since: str = None):
    user, err = _auth_check(request)
    if err:
        return err
    trades = load_trades_from_db("demo", since=since)
    return JSONResponse({"all_trades": trades, "rejected_trades": []})


@app.get("/api/live/stats")
async def api_live_stats(request: Request):
    user, err = _auth_check(request)
    if err:
        return err
    stats = compute_stats_from_db("live")
    if stats:
        return JSONResponse(stats)
    return JSONResponse({
        "total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0,
        "total_pnl": 0, "elapsed_hours": 0, "trades_per_hour": 0,
        "rejected_trades": 0, "current_window": 0,
        "cum_pnl_series": [], "hourly_pnl": {},
        "profit_factor": None, "max_drawdown": 0,
    })


@app.get("/api/live/trades")
async def api_live_trades(request: Request):
    user, err = _auth_check(request)
    if err:
        return err
    trades = load_trades_from_db("live")
    return JSONResponse({"all_trades": trades, "rejected_trades": []})


@app.post("/api/live/kill", include_in_schema=False)
async def api_live_kill(request: Request):
    user = get_user(request)
    if not user or not is_admin(user):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    try:
        Path("/tmp/KILL_LIVE_BOT").touch()
        return JSONResponse({"ok": True, "message": "Kill switch activated"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/live/unkill", include_in_schema=False)
async def api_live_unkill(request: Request):
    user = get_user(request)
    if not user or not is_admin(user):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    try:
        Path("/tmp/KILL_LIVE_BOT").unlink(missing_ok=True)
        return JSONResponse({"ok": True, "message": "Kill switch cleared"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ─── Live control-server proxy ────────────────────────────────────────────────
# All /api/live/server/* routes proxy to the VPS control server.
# Requires LIVE_SERVER_URL + LIVE_SERVER_SECRET set in Vercel env vars.

async def _proxy(path: str, method: str = "GET", body: dict = None) -> dict:
    """Forward a request to the live control server."""
    if not LIVE_SERVER_URL:
        return {"error": "LIVE_SERVER_URL not configured", "connected": False}
    url = f"{LIVE_SERVER_URL}/{path.lstrip('/')}"
    headers = {"x-secret": LIVE_SERVER_SECRET}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            if method == "POST":
                resp = await client.post(url, json=body or {}, headers=headers)
            else:
                resp = await client.get(url, headers=headers)
        return resp.json()
    except httpx.ConnectError:
        return {"error": "Cannot reach control server", "connected": False}
    except Exception as e:
        return {"error": str(e), "connected": False}


@app.get("/api/live/server/status")
async def live_server_status(request: Request):
    user, err = _auth_check(request)
    if err:
        return err
    data = await _proxy("status")
    data["server_configured"] = bool(LIVE_SERVER_URL)
    return JSONResponse(data)


@app.post("/api/live/server/setup-keys")
async def live_server_setup_keys(request: Request):
    user = get_user(request)
    if not user or not is_admin(user):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    body = await request.json()
    return JSONResponse(await _proxy("setup/keys", "POST", body))


@app.post("/api/live/server/start")
async def live_server_start(request: Request):
    user = get_user(request)
    if not user or not is_admin(user):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    body = await request.json()
    return JSONResponse(await _proxy("bot/start", "POST", body))


@app.post("/api/live/server/stop")
async def live_server_stop(request: Request):
    user = get_user(request)
    if not user or not is_admin(user):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    return JSONResponse(await _proxy("bot/stop", "POST"))


@app.get("/api/live/server/logs")
async def live_server_logs(request: Request):
    user, err = _auth_check(request)
    if err:
        return err
    return JSONResponse(await _proxy("bot/logs?lines=150"))


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
