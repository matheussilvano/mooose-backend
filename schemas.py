from typing import Optional, Literal
from pydantic import BaseModel, EmailStr

class UserCreate(BaseModel):
    email: EmailStr
    password: str
    full_name: Optional[str] = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserRead(BaseModel):
    id: int
    email: EmailStr
    full_name: Optional[str]
    credits: int

    class Config:
        from_attributes = True  # <-- MUDANÇA AQUI


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class TokenData(BaseModel):
    user_id: int
    email: EmailStr


class EnemTextRequest(BaseModel):
    tema: str
    texto: str

class CheckoutSimulateRequest(BaseModel):
    plano: Literal["solo", "intensivo", "unlimited"]