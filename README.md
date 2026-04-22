Leviathan v8 — Whale‑First Insider Scanner
A Python‑based insider‑buying scanner that pulls Form 4 filings from SEC EDGAR, filters for high‑conviction signals, applies fundamental + sector filters, and generates daily reports.

This version focuses on whale‑first detection, optimized EDGAR parsing, and a clean ticker universe.

📦 Installation
1. Clone the repository
bash
git clone https://github.com/Lugnutzzz/Leviathan
cd Leviathan
2. Install dependencies
Make sure Python 3.10+ is installed, then run:

bash
pip install -r requirements.txt
3. Configure SEC EDGAR email
Inside leviathan_v8.py, set your email:

python
EDGAR_EMAIL = "your_email_here"
The SEC requires this for identification.
This does not affect scanning speed or functionality.

🚀 Running the Scanner
From inside the repo folder:

bash
python leviathan_v8.py
You will see output like:

Code
PROJECT LEVIATHAN v8.1 — WHALE-FIRST SCANNER
SPY 200-day MA check...
Scanning EDGAR Form 4 filings...
Parsing XML...
Filtering for officer buys...
The first run may take 3–6 hours depending on EDGAR rate limits.
Subsequent runs are faster because cached data is reused.

📄 Output Files
All reports are saved in the same folder as the script, not in .log or /data.

Look for files like:

Code
leviathan_report_2026-04-21.txt
These contain:

Whale buys (officer Form 4 purchases)

Track A / Track B candidates

Rejection reasons

Sector momentum

SPY 200‑day MA status

Watchlist updates

📁 Project Structure
Code
Leviathan/
│
├── leviathan_v8.py                 # Main scanner
├── leviathan_watchlist.json        # Persistent memory
├── leviathan_report_*.txt          # Daily reports
│
├── data/                           # (Optional) fundamentals
├── universe/                       # (Optional) universe builder
└── backtest/                       # (Optional) backtest engine
Note:  
The backtest engine is optional.
You do NOT need to download it to run the scanner.

🧠 What the Scanner Does
1. SPY 200‑day MA check
Stops new entries during bear regimes.

2. EDGAR Form 4 scanning
Atom feed (Tier 1)

EFTS search (Tier 2)

XML parsing

Officer‑only filtering

Purchase‑only filtering

3. Whale detection
Flags large insider buys (high conviction).

4. Fundamental filters
PEG, D/E, margins, growth, insider ownership.

5. Clean ticker universe
Removes:

Warrants (W)

Units (U)

Rights (R)

SPACs

OTC shells

Tickers > 5 characters

Tickers with numbers

⚠ Notes
Long runtime (4–6 hours) is normal due to SEC rate limits.

Changing the EDGAR email does not break anything.

Backtest folder is optional and not required for scanning.

Reports always save as .txt in the main folder.

🤝 Contributing
Pull requests welcome.
Open an issue for major changes.

📬 Contact
For questions or improvements:
@lugnutz__ on discord
