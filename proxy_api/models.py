from ipaddress import IPv4Address, IPv6Address
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProxyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    port: Optional[int] = Field(default=None, ge=1024, le=65535)
    protocol: Literal["http", "socks5"] = "http"
    username: Optional[str] = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: Optional[str] = Field(default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.!@%+=-]+$")

    @model_validator(mode="after")
    def credentials_together(self):
        if (self.username is None) != (self.password is None):
            raise ValueError("username and password must be supplied together")
        return self


class ProxyRecord(ProxyCreate):
    port: int = Field(ge=1024, le=65535)
    ipv6: IPv6Address


class ProxyView(BaseModel):
    port: int
    protocol: Literal["http", "socks5"]
    ipv4: IPv4Address
    ipv6: IPv6Address
    username: Optional[str]
