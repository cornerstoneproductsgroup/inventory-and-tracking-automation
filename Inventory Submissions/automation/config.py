import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass
class Settings:
    rithum_url: str
    rithum_username: str
    rithum_password: str
    sps_url: str
    sps_username: str
    sps_password: str
    headless: bool
    timeout_ms: int


def _to_bool(value: str, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_settings() -> Settings:
    load_dotenv()

    rithum_url = os.getenv("RITHUM_URL", "https://dsm.commercehub.com/dsm/gotoHome.do")
    rithum_username = os.getenv("RITHUM_USERNAME", "")
    rithum_password = os.getenv("RITHUM_PASSWORD", "")
    sps_url = os.getenv("SPS_URL", "https://commerce.spscommerce.com")
    sps_username = os.getenv("SPS_USERNAME", "")
    sps_password = os.getenv("SPS_PASSWORD", "")
    # Default visible browser for desktop runs; set HEADLESS=true for servers/CI.
    headless = _to_bool(os.getenv("HEADLESS", "false"), default=False)
    timeout_ms = int(os.getenv("TIMEOUT_MS", "30000"))

    if not rithum_username:
        raise ValueError("Missing RITHUM_USERNAME in environment.")
    if not rithum_password:
        raise ValueError("Missing RITHUM_PASSWORD in environment.")
    if not sps_username:
        raise ValueError("Missing SPS_USERNAME in environment.")
    if not sps_password:
        raise ValueError("Missing SPS_PASSWORD in environment.")

    return Settings(
        rithum_url=rithum_url,
        rithum_username=rithum_username,
        rithum_password=rithum_password,
        sps_url=sps_url,
        sps_username=sps_username,
        sps_password=sps_password,
        headless=headless,
        timeout_ms=timeout_ms,
    )
