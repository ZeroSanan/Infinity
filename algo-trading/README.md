# Algo Trading Backtest

## Install and Setup
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run Backtest
```bash
source .venv/bin/activate
python run_backtest.py
```

## View Chart Pyplot
```bash
source .venv/bin/activate
python pyplot-view.py
```

## Trading View Chart
```bash
source .venv/bin/activate
python trading-view.py
```

## Generate Excel Report
```bash
source .venv/bin/activate
python generate_excel_report.py
```

The Excel report will be saved in the `reports/` directory with filename format: `YYYY-MM-DD-HH-MM-SS-[ASSET]-backtest.xlsx`