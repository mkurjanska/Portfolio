"""Pulls interconnection-queue data from grid operators via the gridstatus API, plus reads
the per-ISO CSV exports (14 shared columns: Queue ID, Project Name, Interconnecting Entity,
County, State, Interconnection Location, Transmission Owner, Generation Type, Capacity (MW),
..., Status) into one combined table."""
from pathlib import Path

import gridstatus
import numpy as np
import pandas as pd

ISO_CLASSES = {
    "caiso": gridstatus.CAISO,
    "nyiso": gridstatus.NYISO,
    "spp": gridstatus.SPP,
    "ercot": gridstatus.Ercot,
    "miso": gridstatus.MISO,
    "isone": gridstatus.ISONE,
}


def download_iso_queues(iso_classes=ISO_CLASSES, out_dir="iso_data"):
    """Pull each ISO's interconnection queue and save to CSV. Grid operators expose this data
    with wildly different reliability -- some rate-limit or block scrapers outright, some
    (PJM) require an API key -- so each pull is isolated: one operator's failure doesn't stop
    the rest. PJM is skipped here rather than run with a placeholder key."""
    for name, cls in iso_classes.items():
        try:
            queue = cls().get_interconnection_queue()
            queue.to_csv(f"{out_dir}/{name}_interconnection_queue.csv", index=False)
            print(f"{name}: saved {len(queue)} rows")
        except Exception as e:
            print(f"{name}: failed ({e})")


def read_all_files(folder_path):
    """Read every per-ISO CSV in folder_path, tag each with its ISO from the filename prefix,
    and return the combined rows plus a per-file row count for a sanity-check total."""
    folder = Path(folder_path)
    file_names = [fn.name for fn in folder.iterdir() if fn.is_file() and fn.suffix.lower() == ".csv"]

    iso_frames = []
    row_counts = []
    for name in file_names:
        data = pd.read_csv(folder / name, encoding="latin1")
        cols_to_add = data.iloc[:, 0:14].copy()
        cols_to_add["ISO"] = name.split("_", 1)[0]
        iso_frames.append(cols_to_add)
        row_counts.append(len(cols_to_add))
        print(f"{name}: {len(cols_to_add)} rows")

    return iso_frames, row_counts


def load_nyiso(path="NYISO_Interconnection_queue.csv", n_rows=161):
    """NYISO's raw export uses different column names than the other ISOs' shared 14-column
    format, and doesn't carry a Status column at all -- its listing only covers active queue
    entries, so Status is filled as a constant rather than left missing."""
    raw = pd.read_csv(path).loc[0:n_rows - 1, :]
    nyiso = pd.DataFrame({
        "Queue ID": raw["Queue Pos."],
        "Project Name": raw["Project Name"],
        "Interconnecting Entity": raw["Developer/Interconnection Customer"],
        "County": raw["County"],
        "State": raw["State"],
        "Interconnection Location": raw["Points of Interconnection"],
        "Transmission Owner": raw["Utility"],
        "Generation Type": raw["Type/ Fuel"],
        "Capacity (MW)": raw[["SP (MW)", "WP (MW)"]].max(axis=1),
        "Summer Capacity (MW)": raw["SP (MW)"],
        "Winter Capacity (MW)": raw["WP (MW)"],
        "Queue Date": raw["Date of IR"],
        "ISO": "nyiso",
        "Status": "ACTIVE",
    })
    return nyiso


def main():
    iso_frames, row_counts = read_all_files("iso_data")
    nyiso = load_nyiso()
    row_counts.append(len(nyiso))

    iso_data = pd.concat(iso_frames + [nyiso], ignore_index=True)
    print(f"combined shape: {iso_data.shape}  (per-file total: {np.sum(row_counts)})")

    iso_data.to_csv("iso_total_data.csv", index=False)


if __name__ == "__main__":
    main()
