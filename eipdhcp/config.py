"""Settings from .env and the option names."""
import getpass
import os
import sys
from dataclasses import dataclass, field

import urllib3
from dotenv import load_dotenv


@dataclass
class Settings:
    host: str = ""
    user: str = ""
    password: str = ""
    verify_ssl: bool = True
    logs_dir: str = "logs"
    runs_dir: str = "runs"
    # DHCP option names / values exactly as the server stores them
    opt_type: str = "scope"
    opt_value_suffix: str = ";"
    opt_router_names: list = field(default_factory=lambda: ["option routers"])
    opt_dns_names: list = field(default_factory=lambda: ["option domain-name-servers"])
    default_space: str = ""
    default_dhcp_server: str = ""


def _names(env, default):
    return [x.strip() for x in os.getenv(env, default).split(",") if x.strip()]


def env_permission_warning(path):
    """Return a warning if the .env file is readable by other users (Linux filesystems only)."""
    try:
        if os.name != "posix" or path.startswith("/mnt/"):
            return None
        if os.stat(path).st_mode & 0o077:
            return f"{path} is readable by other users; run: chmod 600 {path}"
    except OSError:
        pass
    return None


def load_settings(base_dir, need_credentials=True, interactive=True):
    env_file = os.path.join(base_dir, ".env")
    if os.path.exists(env_file):
        load_dotenv(env_file)
    else:
        load_dotenv()
    s = Settings(
        host=os.getenv("EIP_HOST", ""),
        user=os.getenv("EIP_USER", ""),
        password=os.getenv("EIP_PASSWORD", ""),
        verify_ssl=os.getenv("EIP_VERIFY_SSL", "true").lower() == "true",
        logs_dir=os.path.join(base_dir, "logs"),
        runs_dir=os.path.join(base_dir, "runs"),
        opt_type=os.getenv("OPT_TYPE", "scope"),
        opt_value_suffix=os.getenv("OPT_VALUE_SUFFIX", ";"),
        opt_router_names=_names("OPT_ROUTER_NAMES", "option routers"),
        opt_dns_names=_names("OPT_DNS_NAMES", "option domain-name-servers"),
        default_space=os.getenv("EIP_DEFAULT_SPACE", ""),
        default_dhcp_server=os.getenv("EIP_DEFAULT_DHCP_SERVER", ""),
    )
    if not s.verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    if need_credentials:
        if not s.password and interactive and sys.stdin.isatty() and s.host and s.user:
            s.password = getpass.getpass(f"Password for {s.user}@{s.host}: ")
        missing = [n for n, v in (("EIP_HOST", s.host), ("EIP_USER", s.user), ("EIP_PASSWORD", s.password)) if not v]
        if missing:
            sys.exit(f"Missing in .env: {', '.join(missing)}")
    return s
