import os
from datetime import datetime, timedelta
from fastapi import Request, HTTPException, Depends
from fastapi.responses import RedirectResponse, JSONResponse
from authlib.integrations.starlette_client import OAuth
from jose import jwt
from starlette.middleware.sessions import SessionMiddleware

# Configuration
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
ALLOWED_DOMAIN = os.getenv("ALLOWED_DOMAIN", "yourcompany.com")
JWT_SECRET = os.getenv("JWT_SECRET", "change-this-to-a-random-secret-key")
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_HOURS = 24

# OAuth setup
oauth = OAuth()
oauth.register(
    name="google",
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)


def create_jwt_token(email: str, name: str) -> str:
    payload = {
        "sub": email,
        "name": name,
        "exp": datetime.utcnow() + timedelta(hours=JWT_EXPIRATION_HOURS),
        "iat": datetime.utcnow(),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def verify_jwt_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except Exception:
        return None


async def get_current_user(request: Request) -> dict:
    token = request.cookies.get("access_token")
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = verify_jwt_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return payload


def setup_auth_routes(app):

    @app.get("/auth/login")
    async def login(request: Request):
        redirect_uri = request.url_for("auth_callback")
        return await oauth.google.authorize_redirect(request, redirect_uri)

    @app.get("/auth/callback")
    async def auth_callback(request: Request):
        try:
            token = await oauth.google.authorize_access_token(request)
        except Exception as e:
            raise HTTPException(status_code=401, detail=f"OAuth error: {e}")

        user_info = token.get("userinfo")
        if not user_info:
            raise HTTPException(status_code=401, detail="Could not get user info")

        email = user_info.get("email", "")
        name = user_info.get("name", "")

        if not email.endswith(f"@{ALLOWED_DOMAIN}"):
            return JSONResponse(
                status_code=403,
                content={
                    "error": "Access denied",
                    "message": f"Only @{ALLOWED_DOMAIN} accounts are allowed.",
                    "your_email": email,
                },
            )

        jwt_token = create_jwt_token(email, name)
        response = RedirectResponse(url="/ui/index.html")
        response.set_cookie(
            key="access_token",
            value=jwt_token,
            httponly=True,
            secure=False,  # Set True in production with HTTPS
            samesite="lax",
            max_age=JWT_EXPIRATION_HOURS * 3600,
        )
        return response

    @app.get("/auth/me")
    async def get_me(user: dict = Depends(get_current_user)):
        return {"email": user["sub"], "name": user.get("name", "")}

    @app.get("/auth/logout")
    async def logout():
        response = RedirectResponse(url="/auth/login")
        response.delete_cookie("access_token")
        return response
