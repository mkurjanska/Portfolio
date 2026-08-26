"""Texas run of the same facility scraper used for Virginia -- see scrape_facilities.py."""
from scrape_facilities import run

if __name__ == "__main__":
    run(state_name="Texas", state_abbr="TX", csv_file="tx_data_centers_enhanced.csv",
        batch_size=200, resume_from="T5 DATA CENTERS")
