import json
import os
import secrets
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlencode
from urllib.request import Request as UrlRequest, urlopen

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session
from fastapi_mail import FastMail, MessageSchema, ConnectionConfig

import models
from database import SessionLocal
import schemas
from anon_service import (
    free_remaining,
    get_or_create_anon_session,
    merge_anon_to_user,
)
from rate_limiter import enforce_rate_limit
from referrals_service import apply_referral_on_signup, generate_referral_code
from utils import get_client_ip

router = APIRouter(prefix="/auth", tags=["auth"])

# Config JWT
SECRET_KEY = os.environ.get("JWT_SECRET", "mude-esta-chave-em-producao")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 dias

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")
oauth2_scheme_optional = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI")

# Configuração de E-mail (lê do ambiente)
conf = ConnectionConfig(
    MAIL_USERNAME = os.environ.get('MAIL_USERNAME'),
    MAIL_PASSWORD = os.environ.get('MAIL_PASSWORD'),
    MAIL_FROM = os.environ.get('MAIL_FROM'),
    MAIL_PORT = int(os.environ.get('MAIL_PORT', 587)),
    MAIL_SERVER = os.environ.get('MAIL_SERVER'),
    MAIL_STARTTLS = os.environ.get('MAIL_STARTTLS', 'True').lower() == 'true',
    MAIL_SSL_TLS = os.environ.get('MAIL_SSL_TLS', 'False').lower() == 'true',
    USE_CREDENTIALS = True,
    VALIDATE_CERTS = True
)


# ===== DB =====
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ===== Helpers =====
def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def get_user_by_email(db: Session, email: str) -> Optional[models.User]:
    return db.query(models.User).filter(models.User.email == email).first()

# Helper para criar um token de verificação
def create_verification_token(email: str) -> str:
    # Token curto, expira em 1 dia
    expires = datetime.utcnow() + timedelta(days=1)
    to_encode = {"exp": expires, "sub_email": email}
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

# --- NOVO HELPER: Token de redefinição de senha ---
def create_password_reset_token(email: str) -> str:
    # Token mais curto, expira em 1 hora
    expires = datetime.utcnow() + timedelta(hours=1)
    to_encode = {
        "exp": expires,
        "sub_email": email,
        "sub_type": "password_reset" # Para diferenciar de outros tokens
    }
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


# Helper para enviar o e-mail
async def send_verification_email(email: str, token: str):
    backend_url = os.environ.get(
        "BACKEND_URL",
        "https://mooose-backend.onrender.com",
    )
    verification_link = f"{backend_url}/auth/email/confirm?token={token}"

    html = f"""
    <p>Olá!</p>
    <p>Obrigado por se cadastrar na Mooose. Por favor, clique no link abaixo para verificar seu e-mail:</p>
    <p><a href="{verification_link}" style="color: blue; text-decoration: underline;">Verificar meu E-mail</a></p>
    <p>Se você não se cadastrou, por favor ignore este e-mail.</p>
    """

    message = MessageSchema(
        subject="Confirme seu cadastro na Mooose",
        recipients=[email],
        body=html,
        subtype="html"
    )
    
    try:
        fm = FastMail(conf)
        await fm.send_message(message)
    except Exception as e:
        print(f"ERRO AO ENVIAR E-MAIL para {email}: {e}")
        # Em produção, você deve logar isso
        # Não lançar exceção aqui para não travar o registro se o e-mail falhar
        pass

# --- NOVO HELPER: Enviar e-mail de redefinição de senha ---
async def send_password_reset_email(email: str, token: str):
    frontend_url = os.environ.get("FRONTEND_URL", "http://127.0.0.1:5500")
    
    # Aponta para a nova página 'reset-password.html'
    reset_link = f"{frontend_url}/reset-password.html?token={token}"

    html = f"""
    <p>Olá!</p>
    <p>Recebemos uma solicitação para redefinir sua senha na plataforma Mooose. Se não foi você, ignore este e-mail.</p>
    <p>Para criar uma nova senha, clique no link abaixo:</p>
    <p><a href="{reset_link}" style="color: blue; text-decoration: underline;">Redefinir minha Senha</a></p>
    <p>Este link é válido por 1 hora.</p>
    """

    message = MessageSchema(
        subject="Redefinição de senha da Mooose",
        recipients=[email],
        body=html,
        subtype="html"
    )
    
    try:
        fm = FastMail(conf)
        await fm.send_message(message)
    except Exception as e:
        print(f"ERRO AO ENVIAR E-MAIL DE RESET para {email}: {e}")
        pass # Não informe ao usuário se o e-mail falhou, por segurança


def _safe_redirect_path(path: Optional[str]) -> str:
    if path in {"/app/editor", "/app/paywall"}:
        return path
    return "/app/editor"


def _post_login_path(user: models.User) -> str:
    remaining = free_remaining(user.free_used or 0)
    has_credits = (user.credits or 0) > 0
    if remaining > 0 or has_credits:
        return "/app/editor"
    return "/app/paywall"


def _build_frontend_redirect(path: str, token: str) -> str:
    frontend_url = os.environ.get("FRONTEND_URL", "https://mooose.com.br").rstrip("/")
    return f"{frontend_url}{path}?token={token}"


def _google_exchange_code(code: str) -> dict:
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET or not GOOGLE_REDIRECT_URI:
        raise HTTPException(status_code=500, detail="Google OAuth não configurado.")
    payload = urlencode(
        {
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": GOOGLE_REDIRECT_URI,
            "grant_type": "authorization_code",
        }
    ).encode("utf-8")
    req = UrlRequest(
        "https://oauth2.googleapis.com/token",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urlopen(req, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _google_token_info(id_token: str) -> dict:
    url = f"https://oauth2.googleapis.com/tokeninfo?id_token={id_token}"
    with urlopen(url, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> models.User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Não foi possível autenticar. Faça login novamente.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: int = payload.get("sub_id")
        email: str = payload.get("sub_email")
        if user_id is None or email is None:
            raise credentials_exception
        token_data = schemas.TokenData(user_id=user_id, email=email)
    except JWTError:
        raise credentials_exception

    user = db.query(models.User).filter(models.User.id == token_data.user_id).first()
    if user is None:
        raise credentials_exception
    return user


async def get_current_user_optional(
    token: Optional[str] = Depends(oauth2_scheme_optional),
    db: Session = Depends(get_db),
) -> Optional[models.User]:
    if not token:
        return None
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Não foi possível autenticar. Faça login novamente.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: int = payload.get("sub_id")
        email: str = payload.get("sub_email")
        if user_id is None or email is None:
            raise credentials_exception
        token_data = schemas.TokenData(user_id=user_id, email=email)
    except JWTError:
        raise credentials_exception

    user = db.query(models.User).filter(models.User.id == token_data.user_id).first()
    if user is None:
        raise credentials_exception
    return user


# ===== Rotas =====
@router.post("/register", response_model=schemas.UserRead)
async def register(
    user_in: schemas.UserCreate,
    request: Request,
    db: Session = Depends(get_db),
): # Rota agora é ASYNC
    client_ip = get_client_ip(request)
    enforce_rate_limit(f"signup:{client_ip}", limit=5, window_seconds=60)
    existing = get_user_by_email(db, user_in.email)
    if existing:
        raise HTTPException(
            status_code=400, detail="Já existe um usuário com esse e-mail."
        )

    user = models.User(
        email=user_in.email,
        full_name=user_in.full_name,
        hashed_password=get_password_hash(user_in.password),
        credits=2,  # créditos iniciais
        is_verified=False, # NOVO: começa como não verificado
        referral_code=generate_referral_code(db),
        signup_ip=client_ip,
        device_fingerprint=user_in.device_fingerprint,
    )
    db.add(user)
    db.flush()

    apply_referral_on_signup(
        db,
        user,
        user_in.ref,
        signup_ip=client_ip,
        device_fingerprint=user_in.device_fingerprint,
    )

    db.commit()
    db.refresh(user)

    if user_in.anon_id:
        anon_session = get_or_create_anon_session(
            db,
            anon_id=user_in.anon_id,
            ip=client_ip,
            device_id=user_in.device_fingerprint,
        )
        merge_anon_to_user(db, user, anon_session)
        db.commit()
        db.refresh(user)

    # Envia e-mail de verificação
    try:
        token = create_verification_token(user.email)
        await send_verification_email(user.email, token)
    except Exception as e:
        # Se o e-mail falhar, não bloqueie o registro, mas avise no log.
        print(f"ALERTA: Falha ao enviar e-mail de verificação para {user.email}: {e}")

    return user


@router.post("/signup", response_model=schemas.UserRead)
async def signup(
    user_in: schemas.UserCreate,
    request: Request,
    db: Session = Depends(get_db),
):
    return await register(user_in=user_in, request=request, db=db)


@router.post("/login", response_model=schemas.Token)
def login(login_in: schemas.LoginRequest, db: Session = Depends(get_db)):
    user = get_user_by_email(db, login_in.email)
    if not user:
        raise HTTPException(status_code=400, detail="E-mail ou senha inválidos.")
    if not verify_password(login_in.password, user.hashed_password):
        raise HTTPException(status_code=400, detail="E-mail ou senha inválidos.")

    # NOVO: Check de verificação
    if not user.is_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="E-mail não verificado. Por favor, acesse o link enviado para seu e-mail."
        )

    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    token = create_access_token(
        data={"sub_id": user.id, "sub_email": user.email},
        expires_delta=access_token_expires,
    )
    return schemas.Token(access_token=token)


@router.get("/google/start")
def google_start(
    anon_id: Optional[str] = None,
    redirect: Optional[str] = None,
):
    if not GOOGLE_CLIENT_ID or not GOOGLE_REDIRECT_URI:
        raise HTTPException(status_code=500, detail="Google OAuth não configurado.")
    redirect_path = _safe_redirect_path(redirect) if redirect else None
    state_payload = {
        "anon_id": anon_id,
        "redirect": redirect_path,
        "exp": datetime.utcnow() + timedelta(minutes=15),
    }
    state = jwt.encode(state_payload, SECRET_KEY, algorithm=ALGORITHM)
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "prompt": "select_account",
    }
    url = f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"
    return RedirectResponse(url=url)


@router.get("/google/callback")
def google_callback(
    code: str,
    request: Request,
    state: Optional[str] = None,
    db: Session = Depends(get_db),
):
    anon_id = None
    redirect_path = None
    if state:
        try:
            payload = jwt.decode(state, SECRET_KEY, algorithms=[ALGORITHM])
            anon_id = payload.get("anon_id")
            redirect_path = payload.get("redirect")
        except JWTError:
            pass

    token_data = _google_exchange_code(code)
    id_token = token_data.get("id_token")
    if not id_token:
        raise HTTPException(status_code=400, detail="Token Google inválido.")

    info = _google_token_info(id_token)
    email = info.get("email")
    google_id = info.get("sub")
    email_verified = info.get("email_verified") in {"true", True}
    if not email or not google_id:
        raise HTTPException(status_code=400, detail="Dados Google inválidos.")

    user = db.query(models.User).filter(models.User.google_id == google_id).first()
    if not user:
        user = db.query(models.User).filter(models.User.email == email).first()
        if user:
            user.google_id = google_id
        else:
            client_ip = get_client_ip(request) if request else None
            user = models.User(
                email=email,
                full_name=None,
                hashed_password=get_password_hash(secrets.token_urlsafe(32)),
                credits=2,
                is_verified=True,
                referral_code=generate_referral_code(db),
                signup_ip=client_ip,
                google_id=google_id,
            )
            db.add(user)
            db.flush()

    if email_verified and not user.is_verified:
        user.is_verified = True

    if not user.referral_code:
        user.referral_code = generate_referral_code(db)

    if anon_id:
        anon_session = get_or_create_anon_session(
            db,
            anon_id=anon_id,
            ip=get_client_ip(request) if request else None,
            device_id=None,
        )
        merge_anon_to_user(db, user, anon_session)

    db.add(user)
    db.commit()
    db.refresh(user)

    access_token = create_access_token(
        data={"sub_id": user.id, "sub_email": user.email},
    )
    final_redirect = _safe_redirect_path(redirect_path) if redirect_path else _post_login_path(user)
    redirect_url = _build_frontend_redirect(final_redirect, access_token)
    return RedirectResponse(url=redirect_url)


@router.get("/me", response_model=schemas.UserRead)
def read_me(current_user: models.User = Depends(get_current_user)):
    return current_user


@router.get("/email/confirm")
def confirm_email(
    token: str,
    anon_id: Optional[str] = None,
    db: Session = Depends(get_db),
):
    credentials_exception = HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Token de verificação inválido ou expirado.",
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub_email")
        if email is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    user = get_user_by_email(db, email)
    if user is None:
        raise HTTPException(status_code=404, detail="Usuário não encontrado.")

    if not user.is_verified:
        user.is_verified = True

    if anon_id:
        anon_session = get_or_create_anon_session(
            db,
            anon_id=anon_id,
            ip=None,
            device_id=None,
        )
        merge_anon_to_user(db, user, anon_session)

    db.add(user)
    db.commit()
    db.refresh(user)

    access_token = create_access_token(
        data={"sub_id": user.id, "sub_email": user.email},
    )
    redirect_path = _post_login_path(user)
    redirect_url = _build_frontend_redirect(redirect_path, access_token)
    return RedirectResponse(url=redirect_url)


@router.post("/link-anon", response_model=schemas.LinkAnonResponse)
def link_anonymous_session(
    payload: schemas.LinkAnonRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    anon_session = (
        db.query(models.AnonymousSession)
        .filter(models.AnonymousSession.anon_id == payload.anon_id)
        .first()
    )
    if not anon_session:
        return {"linked": False, "free_used": current_user.free_used or 0, "migrated_essays": 0}

    new_used, migrated = merge_anon_to_user(db, current_user, anon_session)
    db.add(current_user)
    db.add(anon_session)
    db.commit()
    db.refresh(current_user)
    return {"linked": True, "free_used": new_used, "migrated_essays": migrated or 0}

# NOVA ROTA: Para verificar o e-mail
@router.post("/verify-email")
def verify_email_route(token: str, db: Session = Depends(get_db)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Token de verificação inválido ou expirado.",
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub_email")
        if email is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    user = get_user_by_email(db, email)
    if user is None:
        raise HTTPException(status_code=404, detail="Usuário não encontrado.")
    
    if user.is_verified:
        return {"message": "E-mail já foi verificado."}

    user.is_verified = True
    db.add(user)
    db.commit()
    
    return {"message": "E-mail verificado com sucesso! Você já pode fazer login."}

# --- NOVA ROTA: Solicitar redefinição de senha ---
@router.post("/forgot-password")
async def forgot_password(
    payload: schemas.ForgotPasswordRequest, 
    db: Session = Depends(get_db)
):
    # Por segurança, NUNCA retorne 404 se o usuário não existir.
    # Apenas não faça nada e retorne 200.
    user = get_user_by_email(db, payload.email)
    
    # Só enviamos se o usuário existir E já tiver verificado o e-mail
    if user and user.is_verified:
        try:
            token = create_password_reset_token(user.email)
            await send_password_reset_email(user.email, token)
        except Exception as e:
            # Log o erro, mas não informe ao usuário
            print(f"ALERTA: Falha ao enviar e-mail de RESET para {user.email}: {e}")
            
    return {"message": "Se uma conta ativa e verificada existir para este e-mail, um link de redefinição foi enviado."}

# --- NOVA ROTA: Redefinir a senha com o token ---
@router.post("/reset-password")
def reset_password(
    payload: schemas.ResetPasswordRequest, 
    db: Session = Depends(get_db)
):
    credentials_exception = HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Token de redefinição inválido ou expirado.",
    )
    try:
        payload_dict = jwt.decode(payload.token, SECRET_KEY, algorithms=[ALGORITHM])
        
        email: str = payload_dict.get("sub_email")
        sub_type: str = payload_dict.get("sub_type")
        
        if email is None or sub_type != "password_reset":
            raise credentials_exception
            
    except JWTError:
        raise credentials_exception

    user = get_user_by_email(db, email)
    if user is None:
        raise HTTPException(status_code=404, detail="Usuário não encontrado.")
    
    # Atualiza a senha
    user.hashed_password = get_password_hash(payload.new_password)
    db.add(user)
    db.commit()
    
    return {"message": "Senha atualizada com sucesso! Você já pode fazer login com a nova senha."}
