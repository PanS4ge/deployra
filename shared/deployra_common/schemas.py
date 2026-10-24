"""Wspólne modele danych (pydantic) wymieniane między komponentami.

Wszystko, co trafia do konfiguracji nginx lub do Dockera, jest tu walidowane
ściśle (domeny, hosty, ścieżki), żeby nie dało się wstrzyknąć dyrektyw.
Wyjątkiem są pola `advanced` — to celowo surowy nginx (jak w NPM),
dostępny tylko dla użytkowników z odpowiednim uprawnieniem.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .envfile import BUILD_SCOPES, RUNTIME_SCOPES, scoped
from .i18n import tr

_HOSTNAME = re.compile(r"^(\*\.)?([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_UPSTREAM_HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]{0,251}[A-Za-z0-9])?$")
_URL_PATH = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*$")
_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$")
_SLUG = re.compile(r"^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$")
_IMAGE = re.compile(r"^[a-z0-9][a-z0-9._/:@-]{0,254}$")
_USERNAME = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")
_PANEL_URL = re.compile(r"^https?://[A-Za-z0-9.:\[\]-]{1,255}(/[A-Za-z0-9._~/-]{0,255})?$")  # bez spacji i średników: trafia do nginx

Port = Annotated[int, Field(ge=1, le=65535)]


def check_domain(value: str) -> str:
    value = value.strip().lower().rstrip(".")
    if len(value) > 253 or not _HOSTNAME.match(value):
        raise ValueError(tr("srv.schema.bad_domain", value=repr(value)))
    return value


def check_upstream_host(value: str) -> str:
    value = value.strip()
    try:
        return str(ipaddress.ip_address(value.strip("[]")))
    except ValueError:
        pass
    if not _UPSTREAM_HOST.match(value):
        raise ValueError(tr("srv.schema.bad_host", value=repr(value)))
    return value


def check_slug(value: str) -> str:
    if not _SLUG.match(value):
        raise ValueError(tr("srv.schema.bad_slug"))
    return value


def check_name(value: str) -> str:
    if not _NAME.match(value):
        raise ValueError(tr("srv.schema.bad_name", value=repr(value)))
    return value


def check_image(value: str) -> str:
    value = value.strip()
    if not _IMAGE.match(value):
        raise ValueError(tr("srv.schema.bad_image"))
    return value


def _no_newlines(value: str) -> str:
    if "\n" in value or "\r" in value or "\x00" in value:
        raise ValueError(tr("srv.schema.no_newlines"))
    return value


# --- Docker -----------------------------------------------------------------


class PortMap(BaseModel):
    container: Port
    host: Port | None = None
    protocol: Literal["tcp", "udp"] = "tcp"
    host_ip: str | None = None

    @field_validator("host_ip")
    @classmethod
    def _ip(cls, v: str | None) -> str | None:
        return str(ipaddress.ip_address(v)) if v else None


class VolumeMount(BaseModel):
    source: str = Field(min_length=1, max_length=255)
    target: str = Field(min_length=1, max_length=255)
    read_only: bool = False

    @field_validator("target")
    @classmethod
    def _abs(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError(tr("srv.schema.abs_container_path"))
        return _no_newlines(v)

    @field_validator("source")
    @classmethod
    def _src(cls, v: str) -> str:
        v = _no_newlines(v)
        if not v.startswith("/") and not _NAME.match(v):
            raise ValueError(tr("srv.schema.volume_source"))
        return v


class ContainerSpec(BaseModel):
    """Bezpieczny podzbiór `docker run` (zamiast surowego `containers.run(**json)`)."""

    name: str
    image: str
    command: list[str] | None = None
    working_dir: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    ports: list[PortMap] = Field(default_factory=list)
    volumes: list[VolumeMount] = Field(default_factory=list)
    network: str | None = "deployra"
    restart: Literal["no", "always", "unless-stopped", "on-failure"] = "unless-stopped"
    labels: dict[str, str] = Field(default_factory=dict)
    memory_mb: int | None = Field(default=None, ge=16, le=262144)
    cpus: float | None = Field(default=None, gt=0, le=256)
    pull: bool = True

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return check_name(v)

    @field_validator("image")
    @classmethod
    def _image(cls, v: str) -> str:
        return check_image(v)

    @field_validator("working_dir")
    @classmethod
    def _workdir(cls, v: str | None) -> str | None:
        if v is not None and not v.startswith("/"):
            raise ValueError(tr("srv.schema.abs_container_path"))
        return v if v is None else _no_newlines(v)

    @field_validator("env")
    @classmethod
    def _env(cls, v: dict[str, str]) -> dict[str, str]:
        for key in v:
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
                raise ValueError(tr("srv.schema.bad_env", key=repr(key)))
        return v


class NetworkSpec(BaseModel):
    name: str
    driver: str = "bridge"
    internal: bool = False
    attachable: bool = True
    enable_ipv6: bool = False
    subnet: str | None = None
    gateway: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    options: dict[str, str] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return check_name(v)

    @field_validator("subnet")
    @classmethod
    def _subnet(cls, v: str | None) -> str | None:
        return str(ipaddress.ip_network(v, strict=False)) if v else None

    @field_validator("gateway")
    @classmethod
    def _gw(cls, v: str | None) -> str | None:
        return str(ipaddress.ip_address(v)) if v else None


class VolumeSpec(BaseModel):
    name: str
    driver: str = "local"
    driver_opts: dict[str, str] = Field(default_factory=dict)
    labels: dict[str, str] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return check_name(v)


class StackSpec(BaseModel):
    compose: str = Field(min_length=1, max_length=512_000)
    env: dict[str, str] = Field(default_factory=dict)


# --- Aplikacje --------------------------------------------------------------


class AppSource(BaseModel):
    kind: Literal["image", "compose", "static", "git", "runtime"]
    image: str | None = None
    compose: str | None = None
    expose_service: str | None = None
    git_url: str | None = None
    git_branch: str = "main"
    git_token: str | None = None
    git_ssh_key: str | None = None
    dockerfile: str = "Dockerfile"
    build_context: str = "."
    git_static_dir: str | None = None  # Git bez Dockerfile: katalog z plikami strony
    command: list[str] | None = Field(default=None, max_length=64)  # obraz: polecenie zamiast CMD obrazu (np. przejęty kontener)
    runtime: str | None = None  # aplikacja z plików: id środowiska z `runtimes.RUNTIMES`
    start_command: str | None = Field(default=None, max_length=2000)  # zamiast domyślnego polecenia środowiska
    install_command: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _check(self) -> "AppSource":
        if self.kind == "image" and not self.image:
            raise ValueError(tr("srv.schema.need_image"))
        if self.kind == "compose" and not self.compose:
            raise ValueError(tr("srv.schema.need_compose"))
        if self.kind == "runtime":
            from .runtimes import RUNTIMES

            if self.runtime not in RUNTIMES:
                raise ValueError(tr("srv.schema.bad_runtime"))
            for command in (self.start_command, self.install_command):
                if command and chr(0) in command:
                    raise ValueError(tr("srv.schema.no_newlines"))
        if self.kind == "git":
            if not self.git_url or not re.match(r"^(https://|git@|ssh://)[^\s]+$", self.git_url):
                raise ValueError(tr("srv.schema.need_repo"))
            if not re.match(r"^[A-Za-z0-9._/-]{1,100}$", self.git_branch):
                raise ValueError(tr("srv.schema.bad_branch"))
            for rel in (self.dockerfile, self.build_context, self.git_static_dir or "."):
                if rel.startswith("/") or ".." in rel.split("/"):
                    raise ValueError(tr("srv.schema.relative_paths"))
        if self.image:
            self.image = check_image(self.image)
        return self


class AppSpec(BaseModel):
    slug: str
    source: AppSource
    endpoint: str = "local"  # Docker hosta albo `ct-<vmid>` (kontener LXC na Proxmoxie)
    container_port: Port | None = None
    host_port: Port | None = None
    env: dict[str, str] = Field(default_factory=dict)
    env_scopes: dict[str, Literal["runtime", "build", "both"]] = Field(default_factory=dict)  # tylko zmienne spoza „runtime”
    volumes: list[VolumeMount] = Field(default_factory=list)
    memory_mb: int | None = Field(default=None, ge=16, le=262144)
    cpus: float | None = Field(default=None, gt=0, le=256)
    domains: list[str] = Field(default_factory=list)
    zero_downtime: bool = False  # nowy kontener startuje obok starego; przełączenie dopiero po teście zdrowia
    health_path: str | None = Field(default=None, max_length=200)  # np. /up — HTTP < 500 = zdrowy; brak = wystarczy „działa”
    refresh_image: bool = False  # jednorazowo (naprawa podatności): obraz środowiska i bazowy Dockerfile'a pobrane od nowa

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str) -> str:
        return check_slug(v)

    @field_validator("domains")
    @classmethod
    def _domains(cls, v: list[str]) -> list[str]:
        return [check_domain(d) for d in v]

    @field_validator("health_path")
    @classmethod
    def _health_path(cls, v: str | None) -> str | None:
        if not v:
            return None
        if not _URL_PATH.match(v):
            raise ValueError(tr("srv.schema.bad_health_path"))
        return v

    def runtime_env(self) -> dict[str, str]:
        """Zmienne działającego kontenera (bez tych, które są tylko do budowania)."""
        return scoped(self.env, self.env_scopes, RUNTIME_SCOPES)

    def build_env(self) -> dict[str, str]:
        """Zmienne budowania: argumenty `docker build` (zakres build i both)."""
        return scoped(self.env, self.env_scopes, BUILD_SCOPES)

    @model_validator(mode="after")
    def _scopes(self) -> "AppSpec":
        self.env_scopes = {k: v for k, v in self.env_scopes.items() if v != "runtime" and k in self.env}
        return self

    @model_validator(mode="after")
    def _ports(self) -> "AppSpec":
        if not re.match(r"^(local|ct-\d{1,9})$", self.endpoint):
            raise ValueError(tr("srv.schema.bad_endpoint"))
        static = self.source.kind in ("static", "runtime") or (self.source.kind == "git" and self.source.git_static_dir)
        if static and self.endpoint != "local":
            raise ValueError(tr("srv.schema.static_local"))
        if self.source.kind in ("image", "compose") and not self.container_port:
            raise ValueError(tr("srv.schema.need_port"))
        if self.source.kind == "git" and not self.source.git_static_dir and not self.container_port:
            raise ValueError(tr("srv.schema.need_port"))
        if self.source.kind == "runtime" and not self.container_port:
            from .runtimes import RUNTIMES

            self.container_port = RUNTIMES[self.source.runtime or ""].port  # None = proces w tle bez portu
        return self


# --- Proxy (neutralny model; renderuje sterownik w Deployra-WG) -------------


class Upstream(BaseModel):
    scheme: Literal["http", "https"] = "http"
    host: str
    port: Port
    path: str = ""

    @field_validator("host")
    @classmethod
    def _host(cls, v: str) -> str:
        return check_upstream_host(v)

    @field_validator("path")
    @classmethod
    def _path(cls, v: str) -> str:
        if v and not _URL_PATH.match(v):
            raise ValueError(tr("srv.schema.bad_target_path"))
        return v


class ProxyLocation(BaseModel):
    path: str
    upstream: Upstream
    advanced: str = ""

    @field_validator("path")
    @classmethod
    def _path(cls, v: str) -> str:
        if not _URL_PATH.match(v):
            raise ValueError(tr("srv.schema.bad_location_path"))
        return v


class Redirect(BaseModel):
    scheme: Literal["auto", "http", "https"] = "auto"
    domain: str
    code: Literal[301, 302, 307, 308] = 302
    preserve_path: bool = True

    @field_validator("domain")
    @classmethod
    def _domain(cls, v: str) -> str:
        return check_domain(v)


class AccessUser(BaseModel):
    username: str
    htpasswd: str  # gotowy hash (apr1/bcrypt/sha), nie hasło

    @field_validator("username")
    @classmethod
    def _user(cls, v: str) -> str:
        if not _USERNAME.match(v):
            raise ValueError(tr("srv.schema.bad_username"))
        return v

    @field_validator("htpasswd")
    @classmethod
    def _hash(cls, v: str) -> str:
        v = _no_newlines(v)
        if ":" in v or not v:
            raise ValueError(tr("srv.schema.bad_hash"))
        return v


class AccessClient(BaseModel):
    address: str
    directive: Literal["allow", "deny"] = "allow"

    @field_validator("address")
    @classmethod
    def _addr(cls, v: str) -> str:
        v = v.strip()
        if v == "all":
            return v
        return str(ipaddress.ip_network(v, strict=False))


class AccessList(BaseModel):
    id: int
    name: str = ""
    satisfy_any: bool = False
    pass_auth: bool = False
    deployra_login: bool = False  # logowanie do panelu Deployra; kto może wejść, decyduje panel
    users: list[AccessUser] = Field(default_factory=list)
    clients: list[AccessClient] = Field(default_factory=list)


class PanelAuth(BaseModel):
    """Adresy panelu dla logowania Deployra przed aplikacją (forward auth)."""

    verify: str  # bramka → panel: sprawdzenie ciasteczka (GET /auth/forward)
    callback: str  # bramka → panel: wymiana jednorazowego tokenu na ciasteczko (GET /auth/forward/callback)
    login: str  # przeglądarka → panel: strona logowania (adres publiczny)

    @field_validator("verify", "callback", "login")
    @classmethod
    def _url(cls, v: str) -> str:
        if not _PANEL_URL.match(v):
            raise ValueError(tr("srv.schema.bad_url"))
        return v


class Site(BaseModel):
    id: int
    kind: Literal["proxy", "redirect", "dead"] = "proxy"
    domains: list[str] = Field(min_length=1)
    upstream: Upstream | None = None
    redirect: Redirect | None = None
    locations: list[ProxyLocation] = Field(default_factory=list)
    cert_id: int | None = None
    ssl_forced: bool = False
    http2: bool = True
    http3: bool = False
    hsts: bool = False
    hsts_subdomains: bool = False
    block_exploits: bool = True
    websockets: bool = True
    caching: bool = False
    trust_forwarded_proto: bool = False
    access_list_id: int | None = None
    advanced: str = ""
    max_body_mb: int = Field(default=100, ge=0, le=100_000)
    enabled: bool = True

    @field_validator("domains")
    @classmethod
    def _domains(cls, v: list[str]) -> list[str]:
        return [check_domain(d) for d in v]

    @model_validator(mode="after")
    def _kind(self) -> "Site":
        if self.kind == "proxy" and self.upstream is None:
            raise ValueError(tr("srv.schema.proxy_needs_upstream"))
        if self.kind == "redirect" and self.redirect is None:
            raise ValueError(tr("srv.schema.redirect_needs_domain"))
        return self


class Stream(BaseModel):
    id: int
    listen_port: Port
    upstream_host: str
    upstream_port: Port
    tcp: bool = True
    udp: bool = False
    enabled: bool = True

    @field_validator("upstream_host")
    @classmethod
    def _host(cls, v: str) -> str:
        return check_upstream_host(v)


class ProxyState(BaseModel):
    """Pełny pożądany stan proxy — bramka renderuje i podmienia całość atomowo."""

    sites: list[Site] = Field(default_factory=list)
    streams: list[Stream] = Field(default_factory=list)
    access_lists: list[AccessList] = Field(default_factory=list)
    default_page: Literal["404", "deployra", "passthrough"] = "deployra"
    default_upstream: Upstream | None = None
    panel_auth: PanelAuth | None = None  # brak = panel nieosiągalny dla bramki; listy z logowaniem zamykają dostęp


class CertRequest(BaseModel):
    id: int
    domains: list[str] = Field(min_length=1, max_length=100)
    challenge: Literal["http", "dns-cloudflare"] = "http"
    email: str
    cloudflare_token: str | None = None
    staging: bool = False

    @field_validator("domains")
    @classmethod
    def _domains(cls, v: list[str]) -> list[str]:
        return [check_domain(d) for d in v]

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v):
            raise ValueError(tr("srv.schema.bad_email"))
        return v

    @model_validator(mode="after")
    def _wildcard(self) -> "CertRequest":
        if any(d.startswith("*.") for d in self.domains) and self.challenge != "dns-cloudflare":
            raise ValueError(tr("srv.schema.wildcard_dns"))
        if self.challenge == "dns-cloudflare" and not self.cloudflare_token:
            raise ValueError(tr("srv.schema.dns_needs_token"))
        return self


class CertUpload(BaseModel):
    id: int
    fullchain: str = Field(min_length=20, max_length=200_000)
    privkey: str = Field(min_length=20, max_length=200_000)


# --- Parowanie / tunel -------------------------------------------------------


class TunnelConfig(BaseModel):
    """To, co węzeł/panel dostaje od bramki po dołączeniu."""

    address: str  # np. 10.66.0.5
    subnet: str  # np. 10.66.0.0/24
    hub_public_key: str
    hub_endpoint: str  # host:port UDP
    hub_address: str  # 10.66.0.1
    gateway_api: str  # http://10.66.0.1:2751
    keepalive: int = 25
