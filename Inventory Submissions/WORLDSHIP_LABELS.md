# WorldShip label save (CornerstoneMaster)

After batch import processing, WorldShip shows **Save Print Output As** once per shipment that saves to the share. The script maps each dialog to the next **SAVE** row in `CornerstoneMaster.csv` / `.xlsx`.

## Required CSV row order

**All SAVE rows at the top, then all warehouse-print rows at the bottom.** Do not put a print row between save rows.

WorldShip shows Save dialogs in the same order as the batch file. Mixed rows caused label 2+ to use the wrong PO/folder. If a SAVE row appears after a print row, the run stops immediately with a clear error.

Example (6 shipments):

1. Row 2 — SAVE (Agra Life)
2. Row 3 — SAVE (Ez Pole)
3. Row 4 — SAVE (Post Protector)
4. Row 6 — SAVE (Post Protector)
5. Row 7 — SAVE (Ez Pole)
6. Row 5 — PRINT warehouse (Cornerstone) — last

## Phases

1. **Phase 1** — Every SAVE row: wait for a **new** Save dialog → set vendor folder → full `PURCHASE_ORDER` filename → Save → verify file on disk and dialog closed. Stops on first failure (no silent skip).
2. **Phase 2** — Warehouse-print rows: wait for print; dismiss any unexpected Save dialog.

## Automatic Processing Progress — do not click Stop

While WorldShip shows **Automatic Processing Progress**, the batch runs shipment-by-shipment. After you save one label, WorldShip must **keep processing** so the next **Save Print Output As** dialog appears.

- **Do not click Stop** — that halts the batch; remaining shipments will not get Save dialogs.
- **Only click Save** on each Save Print Output As window (the script does this when automation runs).
- The script never sends **Alt+S** (that shortcut is **Stop** on the progress window if focus is wrong).

If you already clicked Stop, close the import, re-run the batch, and let processing run through all save labels without stopping.

## Verification

- Filename must match the PO cell before Save is clicked.
- PDF must be **newly written** (not an old file with the same name).
- Save dialog must **close** before the next label.
- Post-save check: correct path, name, and minimum size (`WORLDSHIP_MIN_LABEL_BYTES`, default 800).

## Env tuning

- `WORLDSHIP_SAVE_BETWEEN_LABELS_S` — pause between save dialogs (default 4)
- `WORLDSHIP_SAVE_FOLDER_NAV_S` — wait after folder path Enter (default 1.4)
- `WORLDSHIP_FIRST_SAVE_TIMEOUT_S` / `WORLDSHIP_SAVE_TIMEOUT_S` — wait for dialog
