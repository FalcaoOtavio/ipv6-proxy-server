import secrets
from ipaddress import IPv4Address, IPv6Address
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


OWNER_PATTERN = r"^[A-Za-z0-9._-]{1,64}$"

class ProxyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    port: Optional[int] = Field(default=None, ge=1024, le=65535)
    protocol: Literal["http", "socks5"] = "http"
    username: Optional[str] = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: Optional[str] = Field(default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.!@%+=-]+$")
    owner: Optional[str] = Field(default=None, pattern=OWNER_PATTERN)

    @model_validator(mode="after")
    def credentials_together(self):
        if (self.username is None) != (self.password is None):
            raise ValueError("username and password must be supplied together")
        return self


USERNAME_PATTERN = r"^[A-Za-z0-9_.-]+$"
PASSWORD_PATTERN = r"^[A-Za-z0-9_.!@%+=-]+$"
ID_PATTERN = r"^[0-9a-f]{32}$"

class ProxyUpdate(BaseModel):
    """PATCH body. username/password go together; both null removes auth."""
    model_config = ConfigDict(extra="forbid")

    port: Optional[int] = Field(default=None, ge=1024, le=65535)
    protocol: Optional[Literal["http", "socks5"]] = None
    username: Optional[str] = Field(default=None, min_length=1, max_length=64, pattern=USERNAME_PATTERN)
    password: Optional[str] = Field(default=None, min_length=1, max_length=128, pattern=PASSWORD_PATTERN)

    @model_validator(mode="after")
    def coherent(self):
        sent = self.model_fields_set
        if not sent:
            raise ValueError("nothing to change")
        if ("username" in sent) != ("password" in sent) or (self.username is None) != (self.password is None):
            raise ValueError("username and password must be changed together (both null removes auth)")
        if "port" in sent and self.port is None or "protocol" in sent and self.protocol is None:
            raise ValueError("port and protocol cannot be null")
        return self

class BatchCreate(BaseModel):
    """count proxies at the next free ports from start_port (default: range start), ascending."""
    model_config = ConfigDict(extra="forbid")

    count: int = Field(ge=1)
    start_port: Optional[int] = Field(default=None, ge=1024, le=65535)
    protocol: Literal["http", "socks5"] = "http"
    credentials: Literal["none", "common", "random"] = "none"
    username: Optional[str] = Field(default=None, min_length=1, max_length=58, pattern=USERNAME_PATTERN)
    password: Optional[str] = Field(default=None, min_length=1, max_length=128, pattern=PASSWORD_PATTERN)
    owner: Optional[str] = Field(default=None, pattern=OWNER_PATTERN)
    partial: bool = False

    @model_validator(mode="after")
    def credentials_match_mode(self):
        if self.credentials == "none" and (self.username or self.password):
            raise ValueError("credentials=none takes no username/password")
        if self.credentials == "common" and not (self.username and self.password):
            raise ValueError("credentials=common needs username and password")
        if self.credentials == "random" and self.password:
            raise ValueError("credentials=random generates passwords; username is an optional prefix")
        return self

class ProxyRecord(ProxyCreate):
    port: int = Field(ge=1024, le=65535)
    ipv6: IPv6Address
    # Immutable identity of this proxy: born on create, kept by rotate, never
    # reused when the port is taken again. DELETE/rotate can demand it with
    # If-Match, so a stale request cannot hit whoever inherited the port.
    id: str = Field(default_factory=lambda: secrets.token_hex(16), pattern=ID_PATTERN)


class ProxyView(BaseModel):
    id: str
    port: int
    protocol: Literal["http", "socks5"]
    ipv4: IPv4Address
    ipv6: IPv6Address
    username: Optional[str]
    owner: Optional[str] = None

class ProxyStatusView(ProxyView):
    status: Literal["running", "starting", "failed", "fenced"]
