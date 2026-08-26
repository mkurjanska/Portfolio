"""Joins utility rate data to counties, computing average/std commercial, industrial, and
residential electricity prices by county, state, ownership type, and year."""
import pandas as pd

RATE_COLS = {"comm": "comm_rate", "ind": "ind_rate", "res": "res_rate"}


def add_group_price_stats(df, group_cols, suffix, rate_cols=RATE_COLS):
    """Add avg/std price columns for each rate type, grouped by group_cols."""
    for label, col in rate_cols.items():
        df[f"avg_{label}_price_{suffix}"] = df.groupby(group_cols)[col].transform("mean")
        df[f"std_{label}_price_{suffix}"] = df.groupby(group_cols)[col].transform("std")
    return df


def main():
    data = pd.read_csv("Utility_with_County_data")
    print(data.groupby("county_name")["utility_name"].value_counts())

    # price by county/state, year, ownership type
    data = add_group_price_stats(data, ["county_name", "state_id", "ownership", "year"], "county_ownership")
    # price by county/state, year only
    data = add_group_price_stats(data, ["county_name", "state_id", "year"], "county")

    data.to_csv("Utility_with_County_prices.csv", index=False)
    print(f"saved {len(data)} rows to Utility_with_County_prices.csv")


if __name__ == "__main__":
    main()
