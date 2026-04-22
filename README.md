Leviathan v7 — Insider Momentum Stock Scanner

A clean, beginner‑friendly README for anyone using this project for the first time.

📌 Overview

Leviathan v7 is a Python‑based stock‑selection engine that identifies high‑conviction opportunities using:

Insider Form 4 purchases (SEC EDGAR)

Fundamental filters (PEG, D/E, margins, growth)

Sector momentum (sector ETF vs SPY)

Market regime detection (SPY 200‑day MA)

A clean, pre‑filtered ticker universe

The system generates daily reports and includes a full historical backtest engine.

📁 Project Structure

Leviathan/
│
├── leviathan_v7.py                 # Main daily scanner
├── leviathan_watchlist.json        # Persistent memory across scans
├── leviathan_report_*.txt          # Auto‑generated daily reports
│
├── data/
│   └── edgar_fundamentals.py       # SEC XBRL fundamentals loader
│
├── universe/
│   └── universe_builder.py         # Clean ticker universe generator
│
├── backtest/
│   ├── engine.py                   # Event‑driven backtest engine
│   ├── run_backtest.py             # Main backtest runner
│   ├── additional_factors.py       # Optional extra factors
│   └── parameter_sensitivity.py    # Robustness testing
│
└── README.md

🛠 Installation

1. Install Python

Download Python 3.10+ from: https://www.python.org/downloads/

During installation, check:

Add Python to PATH

2. Install dependencies

Open Command Prompt and run:

pip install yfinance requests pandas numpy scipy

📦 Setup

1. Create project folders

C:\Leviathan\
C:\leviathan_bt\

Place the files into the structure shown above.

2. Configure EDGAR email

Inside leviathan_v7.py, set:

EDGAR_EMAIL = "your_email_here"

This is required by the SEC for API identification.

🚀 Running the Daily Scanner

From Command Prompt:

cd C:\Leviathan
python leviathan_v7.py

The scanner will:

Check SPY’s 200‑day MA

Identify outperforming sectors

Pull recent Form 4 insider purchases

Apply all filters

Score each stock

Generate a report like:

leviathan_report_2026-04-21.txt

📈 Running the Backtest Engine

First run (downloads EDGAR data):

cd C:\leviathan_bt
python backtest/run_backtest.py

First run: 3–6 hours

Later runs: 20–40 minutes

The backtest includes:

Point‑in‑time fundamentals

Form 4 filing‑date alignment

Survivorship‑bias reduction

Realistic trading costs

Volume limits

Multi‑track testing

🔍 What the Scanner Looks For

Fundamentals

PEG < 0.5

D/E < 0.6

Gross margin > 60%

Revenue growth > 15%

Insider ownership > 20%

Insider Activity

Officer (CEO/CFO/COO)

Open‑market purchase

Large dollar value

Market Context

Sector ETF outperforming SPY (90 days)

SPY above 200‑day MA

Universe Filters

Removes:

Warrants (W)

Units (U)

Rights (R)

Tickers > 5 characters

Tickers with numbers

SPACs / shells

📄 Daily Workflow

Run the scanner

Open the generated report

Review Track A / Track B candidates

Verify insider Form 4 filings (links provided)

Apply your own judgment or position sizing

Manage exits (stop‑loss, take‑profit, max hold)

⚠ Disclaimer

This project is for research and educational purposes only. It does not provide financial advice.

🤝 Contributing

Pull requests are welcome. For major changes, open an issue first to discuss what you’d like to modify.

📬 Contact

For questions or collaboration: your_email_here
