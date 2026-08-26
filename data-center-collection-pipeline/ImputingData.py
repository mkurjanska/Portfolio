"""Cleans and imputes missing values in the merged Virginia data center dataset: normalizes
Status labels, fills Power_Capacity ranges and gaps, fills Hyperscaler flags, imputes
Operational/Approval dates by Status+Hyperscaler group, cleans Grid_Operator names, and
expands each facility into one row per year it was active."""
import numpy as np
import pandas as pd

POWER_CAPACITY_MIDPOINTS = {
    '< 10 MW': 5, '<10 MW': 7.5, '10-25 MW': 17.5, '25-50 MW': 37.5, '25-50': 37.5,
    '50-100 MW': 75, '50-100': 75, '100-250 MW': 175, '100-250': 175,
    '250+ MW': 250, '250+': 250,
}


def clean_status(df):
    df['State'] = df['State'].fillna('VA')
    df['Status'] = df['Status'].replace({'Planned': 'Proposed', 'Operating': 'Operational', 'Opertional': 'Operational'})
    return df


def impute_power_capacity(df):
    """Replaces range strings with their midpoint, then fills any remaining gaps with the
    most frequent value for Hyperscaler vs. non-Hyperscaler facilities separately."""
    df['Power_Capacity'] = df['Power_Capacity'].replace(POWER_CAPACITY_MIDPOINTS).astype(float)

    hyperscaler_mode = df.loc[df['Hyperscaler'] == 'Yes', 'Power_Capacity'].mode()[0]
    nonhyperscaler_mode = df.loc[df['Hyperscaler'] == 'No', 'Power_Capacity'].mode()[0]

    is_hyperscaler = (df['Hyperscaler'] == 'Yes') | (df['Hyperscaler'] == 'Likely')
    df['Power_Capacity'] = np.where(is_hyperscaler & df['Power_Capacity'].isna(), hyperscaler_mode, df['Power_Capacity'])
    df['Power_Capacity'] = np.where((df['Hyperscaler'] == 'No') & df['Power_Capacity'].isna(), nonhyperscaler_mode, df['Power_Capacity'])
    return df


def impute_hyperscaler(df):
    """A facility over 200MW is almost certainly a hyperscale build; below that, absent other
    evidence, default to non-hyperscale."""
    df['Hyperscaler'] = np.where(df['Power_Capacity'] > 200, df['Hyperscaler'].fillna('Likely'), df['Hyperscaler'].fillna('No'))
    return df


def hyperscaler_mode_impute(df, status_value, date_col):
    """Fill missing values in date_col with the most common date, computed separately for
    Hyperscaler ('Yes'/'Likely') vs non-Hyperscaler facilities with the given Status. Same
    logic used for Operational_Date and Approval_Date, just with a different status/column
    pair."""
    is_status = df['Status'] == status_value
    is_hyperscaler = df['Hyperscaler'].isin(['Yes', 'Likely'])
    is_nonhyperscaler = df['Hyperscaler'] == 'No'

    hyperscaler_mode = df[is_status & is_hyperscaler][date_col].mode()
    nonhyperscaler_mode = df[is_status & is_nonhyperscaler][date_col].mode()

    df[date_col] = np.where(
        is_status & is_hyperscaler,
        df[date_col].fillna(hyperscaler_mode[0]),
        df[date_col]
    )
    df[date_col] = np.where(
        is_status & is_nonhyperscaler & df[date_col].isna(),
        df[date_col].fillna(nonhyperscaler_mode[0]),
        df[date_col]
    )
    return df


def clean_grid_operator(df):
    """Collapses Dominion Energy's various listed names into one, fixes two scrape artifacts
    ("...Permit"/"...Permi" suffixes), then fills any remaining gaps with the most frequent
    operator observed in that facility's county."""
    grid_operators = df['Grid_Operator'].unique()
    dominion_variants = [g for g in grid_operators if isinstance(g, str) and 'dominion' in g.lower()]
    df['Grid_Operator'] = np.where(df['Grid_Operator'].isin(dominion_variants), 'Dominion Energy', df['Grid_Operator'])
    df['Grid_Operator'] = df['Grid_Operator'].replace({
        'Manassas Electric SystemPermit': 'Manassas Electric',
        'Appalachian Power CompanyPermi': 'Appalachian Power',
    })

    fallback_mode = df['Grid_Operator'].mode()[0]
    county_mode = {}
    for county in df['County'].unique():
        county_ops = df.loc[df['County'] == county, 'Grid_Operator'].mode()
        county_mode[county] = county_ops[0] if not county_ops.empty else fallback_mode
    df['Grid_Operator'] = df['Grid_Operator'].fillna(df['County'].map(county_mode))
    return df


def expand_facility_row(row_va, start_date, end_date):
    """Duplicate a facility row once per year from start_date+1 through end_date, incrementing
    the Year column each time (the row for start_date itself is the original, unduplicated
    row). Same logic used for Operational facilities (keyed on Operational_Date) and Proposed
    facilities (keyed on Approval_Date) -- only the start date differs."""
    num_times = end_date - start_date
    repeated = np.repeat(row_va.values, num_times, axis=0)
    repeated_df = pd.DataFrame(repeated, columns=row_va.columns)
    for n in range(int(num_times)):
        repeated_df.loc[n, 'Year'] = repeated_df.loc[n, 'Year'] + n + 1
    repeated_df.reset_index(drop=True, inplace=True)
    return repeated_df


def expand_all_facilities(df, end_date=2024):
    """Expands every Operational/Proposed facility into one row per year it was active, up to
    end_date."""
    df['Year'] = np.where(df['Status'] == 'Operational', df['Operational_Date'], df['Approval_Date'])

    expanded = []
    for i in range(len(df)):
        row_va = df.iloc[[i]]
        status = df['Status'].iloc[i]
        if status == 'Operational':
            start_date = df['Operational_Date'].iloc[i]
        elif status == 'Proposed':
            start_date = df['Approval_Date'].iloc[i]
        else:
            continue
        if start_date <= end_date:
            expanded.append(expand_facility_row(row_va, start_date, end_date))

    return pd.concat([df] + expanded, ignore_index=True)


def main():
    va_data = pd.read_csv(
        "DataCenterData_CleaningInProgress/va_clearview_data_centers_enhanced_data.csv",
        encoding='latin-1',
    )
    print(va_data.isnull().sum())

    va_data = clean_status(va_data)
    va_data = impute_power_capacity(va_data)
    va_data = impute_hyperscaler(va_data)

    va_data = hyperscaler_mode_impute(va_data, 'Operational', 'Operational_Date')
    va_data['Operational_Date'] = va_data['Operational_Date'].replace({'TBD': np.nan}).astype(float)
    va_data = hyperscaler_mode_impute(va_data, 'Proposed', 'Approval_Date')
    va_data['Approval_Date'] = va_data['Approval_Date'].astype(float)

    va_data = clean_grid_operator(va_data)
    va_data.to_csv("DataCenterData_CleaningInProgress/va_clearview_data_centers_imputed_data.csv", index=False)
    print(va_data.isnull().sum())

    df_expanded = expand_all_facilities(va_data, end_date=2024)
    df_expanded.to_csv("DataCenterData_CleaningInProgress/va_clearview_data_expanded_data.csv", index=False)
    print(f"expanded {len(va_data)} facilities to {len(df_expanded)} facility-year rows")


if __name__ == "__main__":
    main()
