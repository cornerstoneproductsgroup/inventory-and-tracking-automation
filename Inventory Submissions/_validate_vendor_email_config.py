"""One-off validator — delete after use."""
import json
import sys
from pathlib import Path

p = Path(__file__).resolve().parent / "vendor_email_config.json"
raw = json.loads(p.read_text(encoding="utf-8"))
vendors = [v["vendor_folder"] for v in raw["vendors"]]
dup = sorted({x for x in vendors if vendors.count(x) > 1})
sys.path.insert(0, str(p.parent))
from automation.outlook_vendor_emailer import load_vendor_email_config

cfg = load_vendor_email_config(p)
e = next(v for v in cfg.vendors if v.vendor_folder == "Reach Right USA")
print("JSON OK")
print(f"Vendors: {len(cfg.vendors)}")
print(f"Reach Right USA to: {e.to}")
print(f"Reach Right USA cc: {e.cc}")
print(f"Subject: {e.subject}")
print(f"Dup folders: {dup or 'none'}")
