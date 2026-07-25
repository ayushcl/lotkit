# LotKit

Tool for car-lot photographers: VIN decode, photo packaging, window sticker + buyer's guide PDFs

## Setup

Requires Python 3.11 or newer.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python3 -m uvicorn api.main:app --reload
```

Run the tests:

```bash
python3 -m pytest
```
