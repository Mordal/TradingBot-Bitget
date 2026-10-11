# Bitget spot-bot

Ontwerp en onderbouwing: [Crypto Bot.md](Crypto%20Bot.md). Status: geen bewezen edge, de bot handelt nergens echt.

## Installatie (eenmalig)

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
```

`.env` bevat de sleutels (`BITGET_API_*_DEMO`, `BITGET_API_*_LIVE_READ`). Nooit committen of tonen.

Alle commando's: `.\.venv\Scripts\python -m tradingbot <commando>`

## Commando's

| Commando | Wat het doet |
|---|---|
| `check` | Controleert verbinding, demo-login, private WebSocket en het live-accounttarief (alleen lezen) |
| `history --days 180 --concurrency 4 --rate 8` | Downloadt 1-minuutcandles naar `data/history` (180 dagen duurt ~45 min) |
| `train` | Traint modellen op de historie, toetst ze aan de poort en slaat ze op in `models/` |
| `simulate` | Walk-forward simulatie over de historie, wekelijks hertrainen; rapport in `reports/` |
| `run` | Draait de bot. Standaard shadow: live data, simulator, geen orders. `--minutes N` stopt na N minuten |
| `replay` | Speelt opgenomen live data (`data/recordings`) opnieuw af door de bot |
| `measure` | Meet rate limits en latency (alleen demo). `--ws-only` of `--skip-orders` beperkt de meting |
| `demo-smoke` | Eén kleine BTC-koop en -verkoop op het demo-account |
| `status` | Toont `data/health.json` van de lopende bot |

## Gebruikelijke volgorde

```powershell
.\.venv\Scripts\python -m tradingbot check
.\.venv\Scripts\python -m tradingbot history --days 180 --concurrency 4 --rate 8
.\.venv\Scripts\python -m tradingbot simulate
.\.venv\Scripts\python -m tradingbot run          # laat dit lang draaien: de recorder verzamelt data
.\.venv\Scripts\python -m tradingbot status       # in een tweede terminal
```

## Instellingen en veiligheid

- Alle drempels staan in `config.toml`. Standaard `mode = "shadow"` en `environment = "demo"`.
- `mode = "trade"` plaatst echte orders op de gekozen omgeving. Op live vereist dat `live_confirmed = true` en een aparte `_LIVE`-sleutel met alleen spot trade.
- Noodstop: maak een bestand `KILL` in de projectmap aan. Dan komen er geen nieuwe aankopen meer.
- Een model dat de poort niet haalt handelt niet (`edge_status` in `data/health.json` blijft `shadow`).

## Tests

```powershell
.\.venv\Scripts\python -m pytest -q
```

Data, modellen en rapporten (`data/`, `models/`, `reports/`) staan niet in git.
