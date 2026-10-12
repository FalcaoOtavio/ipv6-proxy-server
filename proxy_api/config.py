import os
from ipaddress import IPv4Address, IPv6Network
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator


class Settings(BaseModel):
    subnet: IPv6Network
    bind_ipv4: IPv4Address
    interface: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,15}$")
    api_token: str = Field(min_length=32)
    data_dir: Path = Path("/var/lib/ipv6-proxy-api")
    binary: Path = Path("/usr/local/bin/3proxy")
    port_start: int = Field(default=30000, ge=1024, le=65535)
    port_end: int = Field(default=39999, ge=1024, le=65535)
    # JSON {"owner": "sha256 hex of that client's token"}; a client token only
    # sees and mutates proxies of its own owner. Hashes only: no secret at rest.
    client_tokens_file: Optional[Path] = None
    # systemd: one ipv6-proxy@<port>.service per proxy (production).
    # local: detached processes with pidfiles, for dev/tests without systemd.
    supervisor: Literal["systemd", "local"] = "systemd"
    batch_max: int = Field(default=1000, ge=1, le=10000)

    @model_validator(mode="after")
    def validate_ranges(self):
        if self.port_end < self.port_start:
            raise ValueError("port_end must be >= port_start")
        if self.subnet.prefixlen > 126:
            raise ValueError("subnet must provide space for rotation (prefix <= 126)")
        if self.bind_ipv4.is_unspecified or self.bind_ipv4.is_multicast:
            raise ValueError("bind_ipv4 must be a specific local IPv4 address")
        if not self.api_token.isascii():
            raise ValueError("api_token must be ASCII")
        return self

    @classmethod
    def from_env(cls):
        names = {
            "subnet": "PROXY_IPV6_SUBNET", "bind_ipv4": "PROXY_BIND_IPV4",
            "interface": "PROXY_INTERFACE", "api_token": "PROXY_API_TOKEN",
            "data_dir": "PROXY_DATA_DIR", "binary": "PROXY_3PROXY_BINARY",
            "port_start": "PROXY_PORT_START", "port_end": "PROXY_PORT_END",
            "client_tokens_file": "PROXY_CLIENT_TOKENS_FILE", "supervisor": "PROXY_SUPERVISOR",
            "batch_max": "PROXY_BATCH_MAX",
        }
        return cls(**{key: os.environ[name] for key, name in names.items() if name in os.environ})
