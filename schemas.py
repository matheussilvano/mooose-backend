from typing import Optional, Literal
from pydantic import BaseModel, EmailStr, conint

class UserCreate(BaseModel):
    email: EmailStr
    password: str
    full_name: Optional[str] = None
    ref: Optional[str] = None
    device_fingerprint: Optional[str] = None


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
    plano: Literal["individual", "padrao", "intensivao"]

# --- NOVAS CLASSES ADICIONADAS ABAIXO ---

class ForgotPasswordRequest(BaseModel):
    email: EmailStr

class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


class EssayReviewCreate(BaseModel):
    essay_id: int
    stars: conint(ge=1, le=5)
    comment: Optional[str] = None


class ReferralStats(BaseModel):
    pending: int
    confirmed: int
    total_earned_credits: int


class ReferralMeResponse(BaseModel):
    referral_code: str
    referral_link: str
    reward_per_referral: int
    stats: ReferralStats


class ReferralActivateResponse(BaseModel):
    credited: bool
    credits_added: int
    reason: Optional[str] = None
