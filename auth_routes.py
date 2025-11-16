import os
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session
from fastapi_mail import FastMail, MessageSchema, ConnectionConfig

import models
from database import SessionLocal
import schemas

router = APIRouter(prefix="/auth", tags=["auth"])

# Config JWT
SECRET_KEY = os.environ.get("JWT_SECRET", "mude-esta-chave-em-producao")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 dias

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

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
    frontend_url = os.environ.get("FRONTEND_URL", "http://127.0.0.1:5500") # Use a URL do seu front na Vercel
    
    # Aponte para a nova página HTML que você criará
    verification_link = f"{frontend_url}/verify-email.html?token={token}"

    html = f"""
    <p>Olá!</p>
    <p>Obrigado por se cadastrar no Cooorrige. Por favor, clique no link abaixo para verificar seu e-mail:</p>
    <p><a href="{verification_link}" style="color: blue; text-decoration: underline;">Verificar meu E-mail</a></p>
    <p>Se você não se cadastrou, por favor ignore este e-mail.</p>
    """

    message = MessageSchema(
        subject="Confirme seu cadastro no Cooorrige",
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
    <p>Recebemos uma solicitação para redefinir sua senha na plataforma Cooorrige. Se não foi você, ignore este e-mail.</p>
    <p>Para criar uma nova senha, clique no link abaixo:</p>
    <p><a href="{reset_link}" style="color: blue; text-decoration: underline;">Redefinir minha Senha</a></p>
    <p>Este link é válido por 1 hora.</p>
    """

    message = MessageSchema(
        subject="Redefinição de senha do Cooorrige",
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


# ===== Rotas =====
@router.post("/register", response_model=schemas.UserRead)
async def register(user_in: schemas.UserCreate, db: Session = Depends(get_db)): # Rota agora é ASYNC
    existing = get_user_by_email(db, user_in.email)
    if existing:
        raise HTTPException(
            status_code=400, detail="Já existe um usuário com esse e-mail."
        )
    user = models.User(
        email=user_in.email,
        full_name=user_in.full_name,
        hashed_password=get_password_hash(user_in.password),
        credits=3,  # créditos iniciais
        is_verified=False # NOVO: começa como não verificado
    )
    db.add(user)
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


@router.get("/me", response_model=schemas.UserRead)
def read_me(current_user: models.User = Depends(get_current_user)):
    return current_user

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