"""Checking scraped Virginia data center records for duplicates/gaps against reference lists,
and merging in a second source (Clearview) to fill missing values. Each stage reads and writes
its own checkpoint CSV, matching how this cleaning pass was actually run."""
import numpy as np
import pandas as pd
from difflib import SequenceMatcher

ENHANCED_CSV = "DataCenterData_CleaningInProgress/va_data_centers_enhanced.csv"


def merge_new_duplicates(enhanced_path, duplicates_path):
    """Duplicates found during scraping sometimes include facilities missing from the main
    enhanced file entirely -- add those in, then re-dedupe."""
    va_df = pd.read_csv(enhanced_path)
    duplicates = pd.read_csv(duplicates_path).drop_duplicates()
    existing_keys = set(map(tuple, va_df[["Facility_Name", "Full_Address"]].values))
    is_new = ~duplicates[["Facility_Name", "Full_Address"]].apply(tuple, axis=1).isin(existing_keys)
    new = duplicates[is_new]
    merged = pd.concat([va_df, new], ignore_index=True).drop_duplicates()
    merged.to_csv(enhanced_path, index=False)
    print(f"merge_new_duplicates: added {len(new)} rows, {len(merged)} total")
    return merged


def find_missing_exact(enhanced_path, reference_path, cols=("Facility_Name", "County")):
    """Facilities in the full reference list with no exact match in the enhanced file."""
    va_df = pd.read_csv(enhanced_path)
    reference = pd.read_csv(reference_path).drop_duplicates()
    merged = reference.merge(va_df[list(cols)], on=list(cols), how="left", indicator=True)
    missing = merged[merged["_merge"] == "left_only"]
    print(f"find_missing_exact: {len(missing)} of {len(reference)} unmatched")
    return missing


def similarity(a, b):
    return SequenceMatcher(None, str(a).lower().replace(",", ""), str(b).lower().replace(",", "")).ratio() * 100


def fuzzy_merge(df1, df2, cols=("Facility_Name", "County"), threshold=80):
    """Best fuzzy match in df2 for every row in df1, averaged across cols."""
    matches = []
    for i, row1 in df1.iterrows():
        best_match = None
        best_score = 0
        for j, row2 in df2.iterrows():
            scores = [similarity(row1[c], row2[c]) for c in cols]
            avg_score = np.mean(scores)
            if avg_score > best_score:
                best_score = avg_score
                best_match = {"j": j, "scores": scores, "avg": avg_score}
        matches.append({"i": i, **best_match})
    return pd.DataFrame(matches)


def find_missing_fuzzy(enhanced_path, reference_path, threshold=80):
    """Facilities in the full reference list with no fuzzy match above threshold in the
    enhanced file -- catches near-duplicates (typos, formatting differences) that an exact
    match on find_missing_exact misses."""
    va_df = pd.read_csv(enhanced_path)
    reference = pd.read_csv(reference_path).drop_duplicates()
    results = fuzzy_merge(reference, va_df, threshold=threshold)
    matched = results[results["avg"] > threshold]
    missing = reference[~reference.index.isin(matched["i"])]
    print(f"find_missing_fuzzy: {len(missing)} of {len(reference)} unmatched at >{threshold}% similarity")
    return missing


def merge_identified_missing(enhanced_path, missing_path):
    """Facilities confirmed missing (by find_missing_exact/find_missing_fuzzy, reviewed by
    hand) get appended directly."""
    va_df = pd.read_csv(enhanced_path, encoding="latin-1")
    missing = pd.read_csv(missing_path, encoding="latin-1")
    merged = pd.concat([va_df, missing], ignore_index=True)
    merged.to_csv(enhanced_path, index=False)
    print(f"merge_identified_missing: added {len(missing)} rows, {len(merged)} total")
    return merged


def merge_clearview_data(enhanced_path, clearview_path, out_path):
    """Merges in a second source (Clearview) covering the same facilities under different
    column names, keeping only the Clearview rows not already present by Facility_Name."""
    va_df = pd.read_csv(enhanced_path, encoding="latin-1")
    clearview = pd.read_csv(clearview_path, encoding="latin-1")

    clearview_va = clearview[clearview["State"] == "VA"].copy()
    clearview_va["County"] = clearview_va["County"].str.replace(" County", "", regex=False)
    clearview_va = clearview_va.rename(columns={
        "Project name": "Facility_Name",
        "Operating year": "Operational_Date",
        "Power capacity (MW)": "Power_Capacity",
    })
    clearview_va[["Full_Address", "Zip_Code", "Grid_Operator", "Hyperscaler",
                  "Application_Date", "Approval_Date"]] = "NA"
    clearview_va["Facility_Name"] = clearview_va["Facility_Name"].str.title()

    va_df["State"] = va_df["State"].str.replace("Virginia", "VA", regex=False)
    va_df["Facility_Name"] = va_df["Facility_Name"].str.title()
    va_df["Full_Address"] = va_df["Full_Address"].str.title()
    va_df = va_df.drop_duplicates(subset=["Facility_Name", "County", "Full_Address"])
    clearview_va = clearview_va.drop_duplicates(subset=["Facility_Name", "County"])
    clearview_va = clearview_va[va_df.columns]

    clearview_dupes = clearview_va.merge(va_df, on=["Facility_Name"], how="inner")
    clearview_new = clearview_va[~clearview_va["Facility_Name"].isin(clearview_dupes["Facility_Name"])]

    merged = pd.concat([va_df, clearview_new], ignore_index=True)
    merged["County"] = merged["County"].str.replace(" County", "", regex=False)
    possible_dupes = merged[merged.duplicated(subset=["Facility_Name", "County"], keep=False)]

    merged.to_csv(out_path, index=False)
    possible_dupes.to_csv("va_data_center_possible_duplicates.csv", index=False)
    print(f"merge_clearview_data: {len(va_df)} existing + {len(clearview_new)} new from "
          f"Clearview = {len(merged)}, {len(possible_dupes)} possible duplicates flagged")
    return merged


def fill_from_clearview_duplicates(data_path, duplicates_path, out_path):
    """For facilities flagged as duplicates between the two sources, fill gaps in
    Operational_Date and Power_Capacity from Clearview's values, and replace broad
    Power_Capacity ranges (e.g. "100-250 MW") with Clearview's more precise figure."""
    data = pd.read_csv(data_path, encoding="latin-1")
    dupes = pd.read_csv(duplicates_path, encoding="latin-1")
    dupes["County"] = dupes["County"].str.replace(" County", "", regex=False)
    dupes["Facility_Name"] = dupes["Facility_Name"].str.title()
    dupes["Project name"] = dupes["Project name"].str.title()
    dupes = dupes.drop_duplicates(subset=["Facility_Name"], keep="first")

    op_map = dupes.set_index("Project name")["Operating year"]
    cap_map = dupes.set_index("Project name")["Power capacity (MW)"]

    mask = data["Facility_Name"].isin(op_map.index) & (data["Operational_Date"].isna() | (data["Operational_Date"] == ""))
    data.loc[mask, "Operational_Date"] = data.loc[mask, "Facility_Name"].map(op_map)

    mask_cap = data["Facility_Name"].isin(cap_map.index) & (data["Power_Capacity"].isna() | (data["Power_Capacity"] == ""))
    data.loc[mask_cap, "Power_Capacity"] = data.loc[mask_cap, "Facility_Name"].map(cap_map)

    mask_better = (data["Facility_Name"].isin(cap_map.index)
                   & data["Power_Capacity"].str.contains(r"[><+\-]", na=False)
                   & data["Facility_Name"].map(cap_map).notna())
    data.loc[mask_better, "Power_Capacity"] = data.loc[mask_better, "Facility_Name"].map(cap_map)

    data.to_csv(out_path, index=False)
    print(f"fill_from_clearview_duplicates: filled {mask.sum()} operational dates, "
          f"{mask_cap.sum()} power capacities")
    return data


def drop_exact_duplicates(path, cols=("Facility_Name", "Full_Address")):
    """Drops rows that are exact duplicates on cols. A fuzzy address comparison (name/county/
    address, all fuzzy) would catch near-duplicates this misses -- not implemented; the
    remaining rows were checked by hand against address instead. fuzzy_merge() above is the
    working technique for that, if it's needed later."""
    df = pd.read_csv(path, encoding="latin-1")
    deduped = df.drop_duplicates(subset=list(cols), keep=False)
    deduped.to_csv(path, index=False)
    print(f"drop_exact_duplicates: {len(df)} -> {len(deduped)} rows")
    return deduped


if __name__ == "__main__":
    merge_new_duplicates(ENHANCED_CSV, "va_data_center_duplicates.csv")
    find_missing_exact(ENHANCED_CSV, "Virginia_Full_List.csv")
    find_missing_fuzzy(ENHANCED_CSV, "Virginia_Full_List.csv")
    merge_identified_missing(ENHANCED_CSV, "DataCenterData_CleaningInProgress/va_missing_facilities 2.csv")
    merge_clearview_data(
        "DataCenterData_CleaningInProgress/va_data_centers_enhanced_.csv",
        "DataCenterData_CleaningInProgress/clearview_data_planned_119.csv",
        "va_clearview_data_centers_enhanced.csv",
    )
    fill_from_clearview_duplicates(
        "DataCenterData_CleaningInProgress/va_clearview_data_centers_enhanced_.csv",
        "DataCenterData_CleaningInProgress/va_clearview_duplicates.csv",
        "DataCenterData_CleaningInProgress/va_clearview_data_centers_enhanced_data.csv",
    )
    drop_exact_duplicates("DataCenterData_CleaningInProgress/va_clearview_data_centers_enhanced_.csv")
