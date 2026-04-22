# Project Title

![GitHub license](https://img.shields.io/badge/license-MIT-blue.svg)
![Build Status](https://img.shields.io/badge/build-passing-brightgreen.svg)

## Description

Leviathan v8 — Whale‑First Insider Scanner
A Python‑based insider‑buying scanner that pulls Form 4 filings from SEC EDGAR, filters for high‑conviction signals, applies fundamental + sector filters, and generates daily reports.

This version focuses on whale‑first detection, optimized EDGAR parsing, and a clean ticker universe.

## Features

- SPY 200‑day MA check
  
Stops new entries during bear regimes.

- EDGAR Form 4 scanning
  
Atom feed (Tier 1), 
EFTS search (Tier 2), 
XML parsing, 
Officer‑only filtering, 
Purchase‑only filtering, 
Whale detection, 
Flags large insider buys (high conviction).

- Fundamental filters
  
PEG, D/E, margins, growth, insider ownership.

- Clean ticker universe
  
Removes:
Warrants (W), 
Units (U), 
Rights (R), 
SPACs, 
OTC shells, 
Tickers > 5 characters, 
Tickers with numbers, 
## Installation

1. Clone the repository


```bash
git clone https://github.com/Lugnutzzz/Leviathan
cd Leviathan
```
2. Install dependencies


```bash
pip install -r requirements.txt
```
3. Configure SEC EDGAR email


Inside leviathan_v8.py, set your email:

```python
EDGAR_EMAIL = "your_email_here"
```
The SEC requires this for identification.
This does not affect scanning speed or functionality.

## Usage

From inside the repo folder:

```bash
python leviathan_v8.py
```
You will see output such as:

Code
PROJECT LEVIATHAN v8.1 — WHALE-FIRST SCANNER
SPY 200-day MA check...
Scanning EDGAR Form 4 filings...
Parsing XML...
Filtering for officer buys...

The first run may take a while depending on EDGAR rate limits.

Subsequent runs are faster because cached data is reused.

Output Files
All reports are saved in the same folder as the script, not in .log or /data.
Look for files like:

leviathan_report_2026-04-21.txt

These contain:
Whale buys (officer Form 4 purchases), 
Track A / Track B candidates, 
Rejection reasons, 
Sector momentum, 
SPY 200‑day MA status, 
Watchlist updates, 

## Contributing

Pull requests welcome.

Open an issue for major changes.

## Contact

For questions or improvements:

@lugnutz__ on discord

## License

This project is licensed under the MIT License.
