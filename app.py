"""
Simple FastAPI login app using starsessions with an Aerospike session store.

Run:
    uvicorn app:app --reload --port 8001
"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware import Middleware
from starsessions import SessionAutoloadMiddleware, SessionMiddleware

from aerospike_store import AerospikeSessionStore

# --- Fake user database (replace with real auth) ---
USERS_DB = {
    "admin": "secret",
    "alice": "password123",
}

# --- Aerospike session store ---
AEROSPIKE_HOST = os.getenv("AEROSPIKE_HOST", "127.0.0.1")
AEROSPIKE_PORT = int(os.getenv("AEROSPIKE_PORT", "3000"))

store = AerospikeSessionStore(
    hosts=[(AEROSPIKE_HOST, AEROSPIKE_PORT)],
    namespace="test",
    set_name="fastapi_sessions",
)

SESSION_LIFETIME = 3600 * 24  # 24 hours


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    store.close()


app = FastAPI(
    lifespan=lifespan,
    middleware=[
        Middleware(
            SessionMiddleware,
            store=store,
            lifetime=SESSION_LIFETIME,
            cookie_https_only=False,
            cookie_name="session_id",
        ),
        Middleware(SessionAutoloadMiddleware),
    ],
)

# --- HTML templates (inline for simplicity) ---

BASE_STYLE = """
<style>
    * { margin:0; padding:0; box-sizing:border-box; }
    body { font-family:system-ui,sans-serif; background:#f0f2f5; color:#333; }
    .navbar { background:#2c3e50; padding:1rem 2rem; display:flex;
              justify-content:space-between; align-items:center; }
    .navbar a,.navbar span { color:#ecf0f1; text-decoration:none; margin-left:1rem; }
    .navbar .brand { font-size:1.2rem; font-weight:bold; }
    .container { max-width:420px; margin:3rem auto; padding:2rem;
                 background:#fff; border-radius:8px; box-shadow:0 2px 10px rgba(0,0,0,.1); }
    h2 { margin-bottom:1.5rem; text-align:center; }
    label { display:block; margin-bottom:.3rem; font-weight:600; }
    input[type=text],input[type=password] { width:100%; padding:.5rem;
        border:1px solid #ccc; border-radius:4px; margin-bottom:1rem; }
    button { background:#3498db; color:#fff; border:none; padding:.6rem 1.5rem;
             border-radius:4px; cursor:pointer; font-size:1rem; width:100%; }
    button:hover { background:#2980b9; }
    .error { color:#e74c3c; text-align:center; margin-bottom:1rem; }
    .links { text-align:center; margin-top:1rem; }
    .links a { color:#3498db; }
</style>
"""


def navbar(username: str | None = None) -> str:
    if username:
        right = f'<span>Hi, {username}</span><a href="/logout">Logout</a>'
    else:
        right = '<a href="/login">Login</a>'
    return f"""
    <nav class="navbar">
        <span class="brand">🔐 FastAPI App</span>
        <div>{right}</div>
    </nav>"""


def page(title: str, body: str, username: str | None = None) -> str:
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
    <title>{title}</title>{BASE_STYLE}</head>
    <body>{navbar(username)}<div class="container">{body}</div></body></html>"""


# --- Routes ---

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    username = request.session.get("username")
    if not username:
        return RedirectResponse("/login", status_code=302)
    body = f"<h2>Welcome, {username}! 🎉</h2><p style='text-align:center'>You are logged in.</p>"
    return page("Home", body, username)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    if request.session.get("username"):
        return RedirectResponse("/", status_code=302)
    err = f'<p class="error">{error}</p>' if error else ""
    body = f"""
    <h2>Login</h2>{err}
    <form method="post" action="/login">
        <label>Username</label><input type="text" name="username" required>
        <label>Password</label><input type="password" name="password" required>
        <button type="submit">Login</button>
    </form>"""
    return page("Login", body)


@app.post("/login")
async def login(request: Request, username: str = Form(), password: str = Form()):
    if USERS_DB.get(username) == password:
        request.session["username"] = username
        return RedirectResponse("/", status_code=302)
    return RedirectResponse("/login?error=Invalid+credentials", status_code=302)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)
