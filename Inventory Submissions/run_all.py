import sys

from automation.rithum import run_rithum_inventory_update
from automation.sps import run_sps_inventory_update


def main() -> None:
    errors = []

    print("=== Starting Rithum inventory update ===")
    try:
        run_rithum_inventory_update()
    except Exception as exc:
        print(f"[ERROR] Rithum failed: {exc}")
        errors.append(f"Rithum: {exc}")

    print("=== Starting SPS Commerce (Tractor Supply) inventory update ===")
    try:
        run_sps_inventory_update()
    except Exception as exc:
        print(f"[ERROR] SPS failed: {exc}")
        errors.append(f"SPS: {exc}")

    if errors:
        print("\nSome automations failed:")
        for err in errors:
            print(f"  - {err}")
        sys.exit(1)
    else:
        print("\nAll inventory updates completed successfully.")


if __name__ == "__main__":
    main()
