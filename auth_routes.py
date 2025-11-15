# auth_routes.py
import os
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

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
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=15))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def get_user_by_email(db: Session, email: str) -> Optional[models.User]:
    return db.query(models.User).filter(models.User.email == email).first()


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
def register(user_in: schemas.UserCreate, db: Session = Depends(get_db)):
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
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.post("/login", response_model=schemas.Token)
def login(login_in: schemas.LoginRequest, db: Session = Depends(get_db)):
    user = get_user_by_email(db, login_in.email)
    if not user:
        raise HTTPException(status_code=400, detail="E-mail ou senha inválidos.")
    if not verify_password(login_in.password, user.hashed_password):
        raise HTTPException(status_code=400, detail="E-mail ou senha inválidos.")

    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    token = create_access_token(
        data={"sub_id": user.id, "sub_email": user.email},
        expires_delta=access_token_expires,
    )
    return schemas.Token(access_token=token)


@router.get("/me", response_model=schemas.UserRead)
def read_me(current_user: models.User = Depends(get_current_user)):
    return current_user
