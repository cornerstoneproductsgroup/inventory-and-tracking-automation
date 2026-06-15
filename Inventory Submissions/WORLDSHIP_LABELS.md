# WorldShip label save (CornerstoneMaster)

After batch import processing, WorldShip shows **Save Print Output As** once per shipment that saves to the share. The script maps each dialog to the next **SAVE** row in `CornerstoneMaster.csv` / `.xlsx`.

## Required CSV row order

**All SAVE rows at the top, then all warehouse-print rows at the bottom.** Do not put a print row between save rows.

SAVE vs print is read from the **LABEL_PR** column in CornerstoneMaster (column X by default):

- **LabelPDF** — save label PDF to the share (phase 1)
- **Label1** — warehouse print, no Save dialog (phase 2)

A warehouse vendor on the SKU map does **not** override LABEL_PR. (Example: Cornerstone SKUs with LabelPDF still save to the share even though Cornerstone is a warehouse vendor.)

WorldShip shows Save dialogs in the same order as the batch file. Mixed rows caused label 2+ to use the wrong PO/folder. If a SAVE row appears after a print row, the run stops immediately with a clear error.

Example (6 shipments):

1. Row 2 — SAVE (Agra Life)
2. Row 3 — SAVE (Ez Pole)
3. Row 4 — SAVE (Post Protector)
4. Row 6 — SAVE (Post Protector)
5. Row 7 — SAVE (Ez Pole)
6. Row 5 — PRINT warehouse (Cornerstone) — last

## Phases

1. **Phase 1** — Every SAVE row: verify PO/SKU/folder → change **folder** → pause **1s** → enter **PO filename** → verify (retry entry if empty) → **Save** → confirm on disk. One full retry only if Save fails.

## Save dialog steps (automation)

1. Save window opens — log expected PO, SKU, folder, filename  
2. Change folder (Alt+D to vendor path)  
3. Pause ~1 second (`WORLDSHIP_SAVE_AFTER_FOLDER_S`)  
4. Enter PO in **File name**  
5. Paste PO, **Tab out** to commit (WM_SETTEXT alone is not enough for this dialog)  
6. Read back field — if empty/wrong, try again (up to 3 times)  
7. Click **Save** only when filename is committed  
8. One retry of the full sequence if the dialog stays open
2. **Phase 2** — Warehouse-print rows: WorldShip prints (may take many minutes); wait for **100%** and **Close**.
3. **Phase 3** — Click **Close** on Automatic Processing Progress when enabled.
4. **Phase 4** — **End of Day** → **Yes** → wait for processing.
5. **Phase 5** — **Import-Export** → **Batch Export** → today’s date → **Next** → preview **Next** → **Save** on summary.

## Automatic Processing Progress — do not click Stop

While WorldShip shows **Automatic Processing Progress**, the batch runs shipment-by-shipment. After you save one label, WorldShip must **keep processing** so the next **Save Print Output As** dialog appears.

- **Do not click Stop** — that halts the batch; remaining shipments will not get Save dialogs.
- **Only click Save** on each Save Print Output As window (the script does this when automation runs).
- The script never sends **Alt+S** (that shortcut is **Stop** on the progress window if focus is wrong).

After each Save, WorldShip often closes that dialog and opens the **next** Save dialog within a few seconds. That is normal — the script tracks the **window handle** of the dialog it just saved and only treats the **same** window staying open as an error (not “any Save dialog visible”).

If you already clicked Stop, close the import, re-run the batch, and let processing run through all save labels without stopping.

## Verification

- Filename must match the PO cell before Save is clicked.
- PDF must be **newly written** (not an old file with the same name).
- Save dialog must **close** before the next label.
- Post-save check: correct path, name, and minimum size (`WORLDSHIP_MIN_LABEL_BYTES`, default 800).

## Env tuning

**Startup wizard pacing** (Import-Export → Batch Import → auto-process → Next → preview Next):

- `WORLDSHIP_AFTER_FOREGROUND_S` — after WorldShip is foreground, before Import-Export tab (default 3)
- `WORLDSHIP_AFTER_IMPORT_EXPORT_TAB_S` — after Import-Export tab, before Batch Import (default 2)
- `WORLDSHIP_AFTER_BATCH_IMPORT_OPEN_S` — after Batch Import, before auto-process checkbox (default 8)
- `WORLDSHIP_BEFORE_NEXT_WAIT_S` — after checkbox, before first Next (default 2)
- `WORLDSHIP_PREVIEW_BEFORE_NEXT_S` — on Import/Export Preview, before Next (default 4)
- `WORLDSHIP_RIBBON_UIA_TIMEOUT_S` — seconds to try UIA before coordinate click fallback (default 2; fixes long gap when log mentions a click but nothing happens)

**Label save phase:**

- `WORLDSHIP_SAVE_BETWEEN_LABELS_S` — pause between save dialogs (default 2)
- `WORLDSHIP_SAVE_AFTER_FOLDER_S` — pause after folder path Enter before typing PO (default 1.0)
- `WORLDSHIP_SAVE_FILENAME_ATTEMPTS` — tries to enter PO if field empty (default 3)
- `WORLDSHIP_SAVE_FOLDER_NAV_S` — max wait for folder to load (default 1.2)
- `WORLDSHIP_SAVE_FILENAME_SETTLE_S` — pause after typing filename (default 0.35)
- `WORLDSHIP_FIRST_SAVE_TIMEOUT_S` / `WORLDSHIP_SAVE_TIMEOUT_S` — wait for dialog
