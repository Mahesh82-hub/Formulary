from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


class OTPRequest(BaseModel):
    email: EmailStr


class OTPRequestResponse(BaseModel):
    challenge_id: UUID
    message: str


class OTPVerifyRequest(BaseModel):
    challenge_id: UUID
    code: str = Field(pattern=r"^\d{6}$")


class UserResponse(BaseModel):
    id: UUID
    email: EmailStr
    display_name: str | None


class AuthenticationResponse(BaseModel):
    user: UserResponse
