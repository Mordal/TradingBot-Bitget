# Bitget Crypto Bot – Kostenbewuste Multi-Agent Daytrading

## 1. Doel en grenzen

Bouw een event-gedreven bot die **alleen spotcrypto** verhandelt op **Bitget**. Een koop is `USDT -> crypto`, een verkoop is `crypto -> USDT`. Geen shorts, margin, leverage, futures of opties. Elke positie is onafhankelijk van de andere: eigen instap, eigen exit, eigen limieten en eigen P&L per instrument.

**Uitgangspunten.**
- **Cashvaluta: USDT.** Bitget biedt USDT-paren met veel meer liquiditeit dan USDC- of EUR-paren (§3.1).
- **Eén beurs.** Data, uitvoering en demo-tests lopen allemaal op Bitget. Een tweede beurs levert op basis van de metingen geen meerwaarde (§3.5). Het ontwerp bevat wel een dunne exchange-interface met één adapter, zodat een tweede beurs later weinig kost.
- **Wetgeving:** dit ontwerp gaat ervan uit dat Bitget voor de gebruiker wettelijk toegestaan is. Dat is niet door mij gecontroleerd; zie §15.
- **Hoofddoel:** zoveel mogelijk winst na kosten, met een overwegend stijgende winstcurve. Het systeem reageert snel, maar **handelt alleen als een verwachte beweging de kosten ruim dekt**. Snelheid is een middel, geen doel.

> **Status van dit document.** Het systeem is gebouwd (§16): 128 geautomatiseerde tests slagen, de keten draait live in shadow-modus en de orderketen is op demo getest. De marktgetallen zijn gemeten op 10 en 11 oktober 2026 met publieke Bitget-data, de demo-omgeving en alleen-lezen aanroepen op het live-account. **De hoofdvraag (netto edge) is met het eerste model negatief beantwoord**: zie §15 punt 1.

---

## 2. Haalbaarheid: kosten, horizon en kernkeuzes

Dit hoofdstuk bepaalt de rest van het ontwerp. Lees het eerst.

### 2.1 De kosten van één rondrit

| Post | Waarde | Bron |
|---|---|---|
| Fee, dit account (spot) | **0,10% maker én taker** | `GET /common/trade-rate` met een live key (alleen lezen), per kandidaat-paar bevestigd |
| Spread (kernparen) | 0,2–5,4 bps | gemeten |
| Slippage | te meten in fase 1; afhankelijk van de diepte (§3.3) | |

Een rondrit kost dus **20 bps fees + spread + slippage ≈ 21–26 bps**. Omdat de maker-fee gelijk is aan de taker-fee, levert een wachtende limietorder **geen kostenbesparing** op; maker-orders hebben alleen nog zin om de spread niet te betalen. Mogelijke kortingen (bv. betalen met het platformtoken BGB) zijn niet gecontroleerd.

**Let op de lijstfee.** De publieke symbolenlijst toont bij 54 oudere crypto-paren (BTC, ETH, SOL, XRP, DOGE, UNI, ZEC, …) een taker-fee van 0,20%. Het werkelijke accounttarief was voor al die paren 0,10%. Voor kostenberekeningen geldt daarom altijd `GET /common/trade-rate`, nooit de lijst.

### 2.2 Waarom 5 seconden niet werkt (en wat wel)

De vraag is niet of de bot snel genoeg is, maar of een koers in korte tijd genoeg beweegt om ~22 bps te dekken. Gemeten over 7 dagen van 1-minuutcandles (standaardafwijking van de 1-minuutrendementen):

| Paar | σ 1 min (bps) | σ per dag, laagste – hoogste | Kans: 5-minutenwinst ≥ kosten (zonder signaal) |
|---|---|---|---|
| NEAR | 19,1 | 13,5 – 26,2 | 23,7% |
| PUMP | 21,1 | 14,8 – 27,6 | 23,1% |
| WLD | 14,7 | 11,2 – 19,9 | 18,3% |
| INJ | 13,4 | 9,3 – 18,7 | 17,5% |
| ONDO | 13,6 | 8,2 – 21,9 | 16,7% |
| ZEC | 12,7 | 7,0 – 19,5 | 16,7% |
| SUI | 11,8 | 8,6 – 16,2 | 15,2% |
| UNI | 11,1 | 6,7 – 16,9 | 13,1% |
| ADA | 12,2 | 10,5 – 15,7 | 13,4% |
| HYPE | 7,7 | 4,8 – 10,5 | 8,7% |
| SOL | 6,4 | 3,9 – 9,7 | 4,4% |
| XRP | 6,6 | 3,7 – 10,2 | 5,3% |
| ETH | 5,1 | 2,5 – 8,1 | 2,6% |
| BTC | 4,1 | 1,7 – 6,0 | 1,8% |

De volatiliteit schommelt sterk van dag tot dag (bv. WLD 11–20 bps), dus de universumselectie moet dynamisch zijn (§3.2).

De **benodigde voorspellende kracht** (correlatie tussen signaal en rendement, "IC") om de kosten terug te verdienen, bij handelen op de sterkste 6,7% signalen (alleen opwaarts, omdat we niet kunnen shorten):

| Paar | 10 min | 30 min | 60 min |
|---|---|---|---|
| NEAR | 0,20 | 0,12 | 0,08 |
| PUMP | 0,20 | 0,11 | 0,08 |
| WLD | 0,26 | 0,15 | 0,11 |
| INJ | 0,26 | 0,15 | 0,11 |
| SUI | 0,29 | 0,17 | 0,12 |
| ONDO | 0,29 | 0,17 | 0,12 |
| ZEC | 0,27 | 0,16 | 0,11 |
| UNI | 0,33 | 0,19 | 0,14 |
| ADA | 0,32 | 0,19 | 0,13 |
| HYPE | 0,43 | 0,25 | 0,18 |
| SOL | 0,53 | 0,31 | 0,22 |
| XRP | 0,51 | 0,29 | 0,21 |
| ETH | 0,64 | 0,37 | 0,26 |
| BTC | 0,79 | 0,45 | 0,32 |

Aannames: normaal verdeelde rendementen, geen drift, signaal in de top 6,7%, kosten = 20 bps + de spread op het meetmoment. Een IC boven ~0,1 is op financiële data zeer zeldzaam (een inschatting, hier niet gemeten). Conclusies:

1. **Een horizon van seconden is niet terug te verdienen**, ook niet met een goed model. De 5-secondenbeweging is op alle paren kleiner dan de kosten.
2. **Haalbaar is alleen** een horizon van **ongeveer 30 tot 120 minuten op de volatiele kernparen**; daar volstaat een IC van ~0,08–0,17. Op 10 minuten is ~0,2–0,3 nodig, wat zeer ambitieus is.
3. **BTC, ETH, SOL en XRP hebben zelden een positieve EV** (IC ≥ 0,2 zelfs op 60 minuten). Ze doen wel mee als de EV-regel (§6.2) hen op een volatiele dag doorlaat; anders zijn ze marktcontext.
4. **Meer transacties maken niet méér winst.** Elke trade kost ~22 bps; meer handel loont alleen als de verwachte winst per trade hoger is.

### 2.3 Kernkeuzes die hieruit volgen

| Keuze | Beslissing |
|---|---|
| Horizon van de modellen | 10, 30, 60 en 120 min (positie typisch 30–180 min) |
| Rol van snelheid | Snel reageren op uitbarstingen, goede instapprijzen, strakke exits; geen handel op ruis |
| Wat het model voorspelt | De kans dat een **take-profit** geraakt wordt vóór een **stop-loss** binnen de horizon, ná kosten (triple-barrier, §10.3) |
| Wat voorspelbaar is | Volatiliteit en volume-uitbarstingen zijn goed voorspelbaar; richting nauwelijks. We handelen alleen bij een expansie van volatiliteit en volume met duidelijke richting |
| Alleen long | Spot kan niet short. Dat halveert het aantal kansen |
| Orderstijl | Instap en stop-loss als marketable limit IOC; take-profit als wachtende limietorder. Maker is niet goedkoper, maar een wachtende take-profit vermijdt de spread en slippage bij de exit |
| Validatie | Live marktdata in **shadow-modus** met een lokale fill-simulator. Het demo-account is niet representatief (§8.1) en dient alleen om de orderketen te testen |
| Go/no-go | Zonder bewezen netto-edge in fase 2 gaat het project niet naar orders (§13) |

---

## 3. Handelsuniversum

### 3.1 Wat Bitget biedt

`GET /spot/public/symbols` geeft 3.382 online spotsymbolen, maar **2.809 daarvan zijn tokenized aandelen** (basismunt met kleine letter, zoals `rAAPL`). Hun volumevelden zijn onbetrouwbaar (bv. 6,8 miljard USD voor één aandeel). Echte crypto: 552 paren.

| Quote | Crypto-paren | ≥ 0,5 M USD / 24 u | ≥ 1 M | ≥ 5 M | Totaal volume (M USD) |
|---|---|---|---|---|---|
| **USDT** | 508 | 80 | 45 | 13 | 395 |
| USDC | 30 | 3 | 0 | 0 | 6 |
| EUR | 11 | 0 | 0 | 0 | 2 |

Alleen USDT is liquide genoeg. Het handelsuniversum bestaat dus uit USDT-paren. Het aantal liquide paren (45 met ≥ 1 M USD per dag) is kleiner dan bij OKX wereldwijd (88), maar groter dan wat een EEA-account op OKX kan handelen: daar zijn alleen USDC- en EUR-paren beschikbaar en bleven vier bruikbare paren over.

### 3.2 Selectiecriteria

Een instrument komt in het handelsuniversum als het voldoet aan **alle** voorwaarden:

1. `status = online`, quote `USDT`, basismunt begint niet met een kleine letter (geen tokenized aandelen), en de basismunt is geen stablecoin of wrapped/staked token.
2. 24u-volume (`quoteVolume`) ≥ `min_volume_24h_usdt` (2 miljoen) en spread ≤ `max_spread_bps` (8).
3. Geen harde volatiliteitsdrempel: elk paar dat de overige criteria haalt, doet mee en de EV-regel (§6.2) beslist per signaal of er winst op te pakken valt. `min_sd1m_bps` (standaard 5) sluit alleen paren uit die de kosten bijna nooit kunnen dekken; een 24u-piek zonder stabiele 7-daagse basis telt niet mee (anomalie-filter, criterium 6).
4. Genoeg diepte: minstens `min_depth_usdt` (5.000) aan liquiditeit binnen 25 bps van de mid aan beide kanten (§3.3).
5. Minimale handelshistorie (`min_listing_age_days`) en geen recente halt of delisting.
6. Uitgesloten als verdacht: extreem hoge volatiliteit of anomalieën in prijs of spread (zie "Afgevallen" in §3.4).

Het universum wordt elke 15 minuten herberekend, met hysterese (wisselen pas na 3 opeenvolgende controles), om de subscribe-limiet te sparen (§8.2).

**Een instrument valt uit het universum terwijl er een positie open staat.** Het universum bepaalt alleen waar nieuwe **BUY's** mogen plaatsvinden, nooit of een bestaande positie beheerd wordt:

- Het instrument gaat naar de status `EXIT_ONLY`: geen nieuwe BUY's en geen bijkopen.
- Alle kanalen (`ticker`, `trade`, `books5`) blijven geabonneerd zolang er een positie of open order is; pas daarna volgt de unsubscribe.
- `AGENT_POSITION_EXIT` blijft draaien met dezelfde take-profit, stop-loss en tijdslimiet. De positie wordt **niet** direct verkocht omdat hij is uitgefilterd, tenzij de reden zelf een noodgeval is (zie hieronder).
- Is de reden van uitval een **noodgeval** (spread of diepte onbruikbaar, handelshalt, delisting, prijsanomalie), dan verkort de exit-logica de tijdslimiet en verkoopt zodra de risicoregels het toelaten, met beperkte stukken (§7).
- Komt het instrument weer binnen het universum, dan wordt `EXIT_ONLY` opgeheven.

### 3.3 Diepte: de echte beperking voor ordergrootte

De bied- en laatzijde zijn bij kleine paren dun. Gemeten orderboekdiepte in USDT (aan één kant) binnen 10 en 25 bps van de mid:

| Paar | Beste prijs (bied / laat) | ≤ 10 bps (bied / laat) | ≤ 25 bps (bied / laat) | Spread (bps) |
|---|---|---|---|---|
| WLD | 44.754 / 17.013 | 97.063 / 68.727 | 262.225 / 190.342 | 5,4 |
| SUI | 3 / 210 | 240.217 / 199.939 | 459.260 / 322.269 | 0,9 |
| ZEC | 159 / 762 | 359.487 / 386.624 | 546.420 / 631.472 | 2,7 |
| HYPE | 242 / 116 | 350.860 / 132.299 | 506.358 / 217.610 | 1,8 |
| ONDO | 231 / 3.220 | 42.859 / 32.421 | 106.458 / 60.519 | 2,0 |
| UNI | 237 / 73 | 27.074 / 15.395 | 192.665 / 63.808 | 1,3 |
| NEAR | 429 / 2.978 | 18.275 / 19.544 | 108.514 / 83.228 | 5,6 |
| ADA | 264 / 715 | 12.875 / 8.755 | 38.571 / 45.952 | 3,9 |
| INJ | 1.908 / 266 | 8.386 / 2.978 | 57.066 / 12.129 | 1,3 |
| PUMP | 1.263 / 4.271 | 9.917 / 16.910 | 31.689 / 39.094 | 9,0 |

(Het orderboek wijzigt continu; dit is één momentopname. De waarden op niveau 1 schommelen sterk.)

Gevolg: orders van enkele honderden tot een paar duizend USDT zijn haalbaar, maar de orderomvang wordt per paar begrensd op een fractie van de beschikbare diepte binnen 10 bps (`max_order_depth_fraction`, standaard 10%). Grotere kapitalen vragen nieuwe afwegingen (§15).

### 3.4 Startlijst (7 dagen, 10 oktober 2026)

**Kern** (voldoet aan de criteria, hoge volatiliteit ten opzichte van de kosten):

| Paar | Volume (M USD/24u) | Spread (bps) | σ 1 min (bps) | IC 30 min | Opmerking |
|---|---|---|---|---|---|
| NEAR-USDT | 6,2 | 3,7 | 19,1 | 0,12 | Hoogste volatiliteit; dunne diepte (§3.3) |
| WLD-USDT | 14,0 | 3,6 | 14,7 | 0,15 | Goede diepte |
| INJ-USDT | 11,6 | 1,3 | 13,4 | 0,15 | Eenzijdig dun orderboek |
| SUI-USDT | 14,0 | 0,9 | 11,8 | 0,17 | Zeer goede diepte, lage spread |
| ZEC-USDT | 10,2 | 0,9 | 12,7 | 0,16 | Goede diepte |
| ONDO-USDT | 2,8 | 4,1 | 13,6 | 0,17 | Matige diepte |
| PUMP-USDT | 2,4 | 5,4 | 21,1 | 0,11 | Hoge spread en dunne diepte |
| UNI-USDT | 2,5 | 2,6 | 11,1 | 0,19 | Matige diepte |
| ADA-USDT | 3,8 | 3,9 | 12,2 | 0,19 | Dunne diepte |

**Doen ook mee, maar zelden een signaal** (hogere benodigde IC): `HYPE-USDT` (IC 30 min 0,25), `DOGE-USDT` (0,26), `SOL-USDT` (0,31), `XRP-USDT` (0,29), `BTC-USDT` (0,45) en `ETH-USDT` (0,37). Het model en de EV-regel bepalen of er een trade komt; er is geen handmatige uitsluiting. BTC en ETH blijven altijd geabonneerd als marktcontext.

**Afgevallen als verdacht:** `US-USDT` (σ 1 min 84,8 bps, 24u-range 8.907 bps), `BTW-USDT` (spread 0,01 bps bij 3,6 M USD volume en σ 20 bps, onnatuurlijk), `UMXM-USDT`, `DGAI-USDT`. Nieuwe en obscure listings zijn dun en kunnen gemanipuleerd zijn; ze komen er alleen in als ze langdurig aan alle criteria voldoen (`min_listing_age_days`).

**Breedte.** Het universum is breder dan de 9 kernparen (alles wat §3.2 haalt, tot `top_n`), maar in de praktijk komen de meeste signalen uit de kernparen. Op een horizon van 30 minuten is dat in de ordegrootte van enkele tientallen kandidaatsignalen per dag, waarvan alleen een deel de EV-drempel haalt; een eerste inschatting is **5–20 trades per dag**. Het doel "veel transacties" is niet haalbaar zonder de selectiviteit weg te gooien.

### 3.5 Waarom niet ook een tweede beurs (OKX)

Gemeten tussen OKX en Bitget (6,6 uur 1-minuutcloses, 398 gelijktijdige minuten per paar, constante stablecoin-offset eruit):

- **Geen arbitrage:** het prijsverschil is bij USDT-tegen-USDT mediaan 0,4–3,7 bps en het 99e percentiel is ≤ 18 bps. Geen enkele minuut lag boven 25 bps, de kosten van een rondrit over twee beurzen (2 × 0,10% + spread).
- **Geen voorloper:** de correlatie met één minuut verschuiving ligt tussen −0,13 en +0,12 zonder vaste richting.
- **Geen kostenvoordeel:** beide rekenen 0,10% taker.

Daarom is er één uitvoeringsbeurs. Publieke OKX-data kan in fase 2 hooguit opnieuw getest worden op een voorloper over een langere periode. De metingen vonden die niet.

---

## 4. Architectuur

```text
   Bitget publieke WebSocket                    Bitget private WebSocket
   ticker, trade, books1/books5                 orders, account (+ trade ops)
              │                                        │
              ▼                                        ▼
     Marktdata + lokale state  ◄────────  Order- en balansreconciliatie (+ REST)
              │                                        ▲
   ┌──────────┼───────────────┬──────────────┐         │
   ▼          ▼               ▼              ▼         │
 UNIVERSE  ORDER_FLOW      EDGE_LGBM       REGIME_     │
 (selectie) (regels)       (model)         FILTER      │
   └──────────┴───────┬───────┴──────────────┘         │
                      ▼                                │
              Beslissingsagent  ◄── POSITION_EXIT (per positie)
                      │                                │
                      ▼                                │
          Risk & Rules Layer (code, onschendbaar)      │
                      │                                │
                      ▼                                │
       USDT-allocator + rate-limit scheduler           │
                      │                                │
                      ▼                                │
         Exchange-interface (adapter: Bitget) ─────────┘
                      │
                      ▼
        Opslag: signalen, beslissingen, orders, fills, metrics
```

De **Risk & Rules Layer** is deterministische code en staat los van de AI. Geen agent kan haar regels overschrijven. Agents adviseren alleen; alleen de keten beslissing → risk → uitvoering kan een order plaatsen.

**Twee snelheden.** De *snelle lus* (marktdata → signalen → beslissing → risk → order) draait volledig in het geheugen, zonder REST-aanroepen en zonder training. De *trage lus* (universumselectie, kalibratie, hertraining, reconciliatie, health) draait op de achtergrond.

---

## 5. Advies-agents

### 5.1 Algemene eisen

- Elke agent heeft een **uniek, stabiel ID** en een `model_version` (model: `v` + trainingstijdstip; regels: `v1`).
- Een agent adviseert per instrument, kent geen portefeuille en geen regels, en ziet nooit data van ná het adviesmoment.
- Elk advies bevat `advice` (`BUY`/`SELL`/`HOLD`), een gekalibreerde `ratio` tussen 0 en 1 en de verwachte netto opbrengst (formaat in §5.3).
- Achteraf wordt beoordeeld of richting én ratio klopten (§10). Het model leert daarvan; regels kalibreren alleen hun ratio.
- Ontbrekende of verouderde input geeft `HOLD` met reden (`stale_data`, `insufficient_depth`, `warming_up`), nooit een gok.

### 5.2 Gedefinieerde agents

| ID | Type | Data | Horizon | Rol |
|---|---|---|---|---|
| `AGENT_UNIVERSE` | Vaste regels (trage lus) | Symbolenlijst, tickers, candles, orderboeken | Elke 15 min | Kiest welke instrumenten actief gevolgd worden (§3.2) |
| `AGENT_ORDER_FLOW` | Vaste regels | `trade`, `books5`, `ticker` | 10–60 min | Herkent volume- en volatiliteitsuitbarstingen met richting |
| `AGENT_EDGE_LGBM` | **Getraind LightGBM-model** | Candle-, trade- en orderboekfeatures | 10, 30, 60 en 120 min | Schat per horizon de kans op take-profit vóór stop-loss, ná kosten |
| `AGENT_REGIME_FILTER` | Vaste regels | Spread, diepte, volatiliteit, BTC-regime, feedkwaliteit | Continu | Bepaalt of een instrument en de markt nu verhandelbaar zijn |
| `AGENT_POSITION_EXIT` | Vaste regels, per positie | Eigen fills + marktstate | Zolang open | Bewaakt take-profit, stop-loss, tijdslimiet en verslechterende edge |

Alleen `AGENT_EDGE_LGBM` is een getraind model. De overige agents zijn uitlegbare regels met drempels in de config. Niets anders wordt gebouwd: geen neurale netwerken, geen LLM, geen nieuws- of sentimentmodel.

#### `AGENT_UNIVERSE` – Welke instrumenten volgen we

- **Input:** `GET /spot/public/symbols`, `GET /spot/market/tickers`, `GET /spot/market/orderbook` en gebufferde candles.
- **Regel:** de criteria uit §3.2; daarbinnen blijft de top `top_n` (standaard 20) op de **kostenbewuste kansscore** `σ_30min / rondrit_kosten` actief, mits diepte en spread binnen de grenzen vallen.
- **Hysterese:** wissel pas na 3 opeenvolgende controles (§8.2).
- **Uitval:** een instrument met een open positie of open order gaat naar `EXIT_ONLY` in plaats van te verdwijnen (§3.2).
- **Output:** `active_instruments` (nieuwe BUY's toegestaan) en `exit_only_instruments` (alleen beheer), met per instrument tick-, lot-, prijs- en minimumregels, diepte en de vooraf berekende kosten.

#### `AGENT_ORDER_FLOW` – Uitbarstingen met richting

- **Input:** `trade` (agressorzijde en volume), `books5` en `ticker`.
- **Berekening:** volume-z-score versus het 30-minuten-gemiddelde; volatiliteitsexpansie (σ over 1 min ÷ σ over 30 min); trade-flow-imbalance; orderboekimbalance op niveau 1 en 1–5; doorbraak boven de hoogste prijs van de laatste 15/60/180 minuten.
- **Advies:** `BUY` bij gelijktijdige volume- en volatiliteitsexpansie, aanhoudende koopdruk en spread onder de limiet; `SELL` bij aanhoudende verkoopdruk (alleen relevant voor een bestaande positie); anders `HOLD`.
- **Rol naast het model:** een onafhankelijke, uitlegbare basislijn en vetostem. Het model moet deze regel in de test verslaan (§10.4).

#### `AGENT_EDGE_LGBM` – De enige model-agent

- **Methode:** één gepoold **LightGBM**-model voor alle instrumenten, met instrumentkenmerken als feature (tick in bps en volatiliteit; de spread zit niet in de features omdat er geen historische spreads bestaan, hij telt wel mee in de kosten) zodat het per instrument kan differiëren. Gekozen omdat de features tabulair zijn, inferentie op CPU in milliseconden gebeurt, weinig data nodig is en feature-importance de uitleg geeft. Geen neuraal netwerk: meer overfitting-risico, geen aantoonbaar voordeel op dit type data.
- **Features** (genormaliseerd in bps of z-scores): returns over 1, 5, 15, 30, 60 min; gerealiseerde volatiliteit over 5, 30, 120 min en hun verhouding (expansie); volume- en tradeaantal-z-score; aandeel agressieve kopers; orderboekimbalance en diepte (alleen waar beschikbaar); spread; afstand tot de hoogste en laagste prijs van 1 en 4 uur; **BTC- en ETH-returns over 5 en 30 min en BTC-volatiliteit** als marktcontext; uur van de dag (cyclisch).
- **Doelen (labels):** triple-barrier per horizon (§10.3): kwam de koers eerst op `+TP`, op `-SL`, of geen van beide binnen de horizon, ná fees en spread.
- **Output:** per horizon de **gekalibreerde kans** `P_win` en de verwachte netto edge in bps: `EV = P_win × TP − (1 − P_win) × SL − kosten`. De agent adviseert `BUY` voor de horizon met de hoogste EV als die boven de drempel ligt.
- **Inferentie:** lokaal, op CPU, gebufferd buiten de event-lus; streefwaarde < 50 ms p99 voor features plus inferentie, gemeten en gelogd.
- **Eerste versie zonder orderboek:** het eerste model wordt getraind op historische 1-minuutcandles en trades (§10.3); orderboekfeatures komen in een tweede versie, op basis van eigen opnames.

#### `AGENT_REGIME_FILTER` – Is dit instrument en deze markt nu verhandelbaar?

- **Input:** spread, diepte binnen 10 en 25 bps, volatiliteit, versheid van de feed, en het BTC-regime (hoge BTC-volatiliteit, snelle BTC-val).
- **Uitkomst per instrument:** `TRADABLE`, `THIN` (kleinere orders), `FAST` (hoge volatiliteit: kleinere omvang en ruimere limiet) of `BLOCKED` (spread of diepte buiten de grenzen, data te oud, instrument in halt, of een scherpe BTC-daling).
- **Rol:** alleen beperken of blokkeren. De filter kan nooit een BUY afdwingen.

#### `AGENT_POSITION_EXIT` – Beheer van elke open positie

- Draait per positie afzonderlijk op elke nieuwe consistente marktstate.
- **Regels** (drempels in de config): *take-profit* op `+TP` (wachtende limietorder); *stop-loss* op `-SL` onder de instapprijs, of trailing zodra de koers `+TP/2` raakte; *tijdslimiet* (`max_hold_seconds`, afgeleid van de gekozen horizon) zonder voldoende winst; *edge-verval* (het model geeft de positie geen positieve EV meer, of spread en diepte worden onbruikbaar).
- **Output:** `HOLD`, `SELL_PARTIAL` of `SELL_ALL` met reden.

### 5.3 Outputstructuur (verplicht voor elke agent)

```json
{
  "agent_id": "AGENT_EDGE_LGBM",
  "model_version": "v20261010T093000",
  "timestamp_ms": 1791617522256,
  "instrument": "WLDUSDT",
  "advice": "BUY",
  "ratio": 0.58,
  "horizon_seconds": 1800,
  "expected_net_edge_bps": 9.4,
  "meta": {
    "tp_bps": 45,
    "sl_bps": 28,
    "p_win_raw": 0.66,
    "cost_bps": 23.6,
    "features_used": ["vol_expansion", "volume_z", "btc_ret_5m", "spread_bps"],
    "data_age_ms": 120,
    "reason": null
  }
}
```

| Veld | Type | Beschrijving |
|---|---|---|
| `agent_id`, `model_version` | string | Welke agent en welke versie |
| `timestamp_ms` | int | Tijdstip van de marktstate (exchange-tijd) |
| `instrument` | string | Bitget-symbool, bv. `WLDUSDT` |
| `advice` | enum | `BUY`, `SELL`, `HOLD` |
| `ratio` | float 0–1 | Gekalibreerde kans dat het advies netto juist is (voor het model: `P_win`) |
| `horizon_seconds` | int | Tijdspanne waarover het advies bedoeld is |
| `expected_net_edge_bps` | float | Verwachte netto opbrengst na kosten |
| `meta` | object | Take-profit, stop-loss, kosten, debuginfo; `reason` bij een `HOLD` door ontbrekende data |

---

## 6. Beslissingsagent

### 6.1 Taak

1. Ontvang de geldige adviezen voor één instrument.
2. Laat agents in *shadow* en agents met een `HOLD` door ontbrekende data buiten beschouwing.
3. Bereken de **verwachte waarde (EV)** en bepaal de actie.
4. Bepaal een **voorstel** voor de omvang in USDT (BUY) of een fractie van de positie (SELL).
5. Geef het voorstel door aan de Risk & Rules Layer.

### 6.2 Beslisregel

Met één model en één regelagent is een geleerd gewichtenmodel overbodig en riskant. Daarom een vaste, uitlegbare regel op basis van verwachte waarde:

```
EV_bps = P_win × TP_bps − (1 − P_win) × SL_bps − kosten_bps        (P_win gekalibreerd, §10.2)
```

| Voorwaarde | Actie |
|---|---|
| `EV_bps ≥ min_ev_bps` én `P_win ≥ min_p_win` én `AGENT_REGIME_FILTER ≠ BLOCKED` én `AGENT_ORDER_FLOW` spreekt niet tegen | **BUY** |
| Exitvoorstel van `AGENT_POSITION_EXIT` | **SELL** (geheel of deels) |
| Anders | **HOLD** |

- **Omvang:** `base_order_usdt × min(1; EV_bps / ev_ref_bps)`; ×0,5 bij `THIN`/`FAST` of als `AGENT_ORDER_FLOW` slechts neutraal is (geen bevestiging); daarna begrensd op een fractie van de diepte (§3.3).
- `kosten_bps = fee_instap + fee_exit + spread + 2 × slippage`, met de fee uit `GET /common/trade-rate`.
- Standaard `min_ev_bps = 5`, `min_p_win = 0,45`, `ev_ref_bps = 15`. Alle drempels staan in de config en worden pas na een replay aangepast (§10.4); de AI wijzigt ze nooit.

### 6.3 Input en output

| Input | Beschrijving |
|---|---|
| `advices[]` | Agent-outputs (§5.3) voor dit instrument |
| `regime` | Uitkomst van `AGENT_REGIME_FILTER` |
| `position` | Eigen positie (hoeveelheid, gemiddelde prijs, open orders) |
| `usdt_available` | Vrije, niet gereserveerde USDT |
| `depth` | Actuele orderboekdiepte van het instrument |
| `fees` | Maker- en taker-fee van het account |

```json
{
  "timestamp_ms": 1791617522300,
  "instrument": "WLDUSDT",
  "action": "BUY",
  "notional_usdt": 100.0,
  "order_style": "limit_ioc",
  "limit_price": 0.5461,
  "tp_bps": 45,
  "sl_bps": 28,
  "ev_bps": 9.4,
  "p_win": 0.58,
  "agent_contributions": { "AGENT_EDGE_LGBM": "BUY/0.58", "AGENT_ORDER_FLOW": "BUY/0.61" },
  "break_even_bps": 23.6,
  "reasoning_log": "korte uitleg voor audit"
}
```

---

## 7. Harde regels (Risk & Rules Layer)

Deze regels zijn **onschendbaar** en worden in gewone code afgedwongen (`risk.py`, `execution.py`), niet door het model. Alle drempels staan in `config.toml`; de AI past ze niet aan.

**Orders**
1. **Alleen spot, alleen USDT-paren.** Nieuwe BUY's alleen in het actieve universum (§3.2); geen instrument dat niet `online` is. Een **bestaande bot-positie** mag altijd verkocht worden, ook als het instrument niet meer in het universum zit (`EXIT_ONLY`, §3.2).
2. **Orderomvang:** maximaal `max_order_usdt` per order en `max_position_usdt` per instrument, en begrensd op `max_order_depth_fraction` van de diepte binnen 10 bps. De omvang wordt afgerond naar `quantityPrecision` en `pricePrecision`, en moet ≥ `minTradeUSDT` zijn; anders geen order. De echte orderwaarde (aantal × limietprijs, na afronding) telt.
3. **Geen shorts.** Een SELL is nooit groter dan de beschikbare eigen hoeveelheid (min. open sell-orders) en gebruikt de **netto** hoeveelheid na fee (§9). Resten onder de minimumorderomvang blijven staan.
4. **Orderstijl:** instap en stop-loss zijn een *limit* met `force = ioc` en een prijs hoogstens `max_slippage_bps` voorbij de beste prijs. De take-profit is een wachtende limietorder (`force = post_only` of `gtc`). Nooit een onbegrensde marktorder in de gewone route; een niet-gevuld restant wordt niet blind opnieuw verstuurd, elke herpoging is een nieuwe risicobeslissing. Bitget begrenst limietprijzen op ±2% van de marktprijs (`buyLimitPriceRatio`/`sellLimitPriceRatio`); prijzen daarbuiten worden geweigerd.
5. **Break-even:** een trade gaat alleen door als `EV_bps ≥ min_ev_bps` (§6.2) na alle kosten. Het fee-tarief komt uit `GET /common/trade-rate` bij opstart en wordt periodiek ververst, niet per trade en niet uit de symbolenlijst (§2.1).
6. **Orderopvolging:** elke order krijgt een unieke `clientOid`. Order-events worden idempotent verwerkt. Een order zonder bevestiging na een timeout gaat naar de reconciliatie in plaats van blind opnieuw te worden verstuurd. Een IOC-order die niet kan vullen krijgt status `cancelled`.

**Portefeuillebescherming (vaste regels, geen model)**
- **USDT-reserve:** `min_usdt_reserve` blijft altijd vrij. Elke BUY reserveert zijn USDT direct; een SELL maakt pas USDT vrij na de fill.
- **Spreiding:** maximaal `max_open_positions` (standaard 6) gelijktijdig, `max_asset_share` van het kapitaal per instrument, en een limiet op de totale blootstelling aan sterk gecorreleerde altcoins (ze bewegen vaak samen met BTC).
- **Drawdown:** gemeten ten opzichte van het hoogste equity van de laatste 24 uur. Meer dan `dd_soft` eronder: orderomvang gehalveerd. Meer dan `dd_hard`: niet meer kopen, enkel nog verkopen.
- **Stop per positie:** elke positie wordt op zijn **eigen prijs** beoordeeld. Zakt hij onder zijn stop, dan wordt **die positie** verkocht, zonder edge-, grootte- of tradelimiet. Daarna volgt `stop_cooldown_s` zonder nieuwe BUY in dat instrument.
- **Dagverlies:** meer dan `daily_loss_pct` onder het begin van de dag stopt het **kopen** voor de rest van de dag. Verkopen blijft altijd mogelijk.
- **Tempo:** maximum aantal BUY's per minuut en per uur, en per instrument één actieve entry- of exit-intentie.
- **Verouderde data:** geen beslissing op marktdata ouder dan `max_data_age_ms` (standaard 2000) of op een inconsistent orderboek.
- **Circuit breaker:** bij een verbroken private verbinding, herhaalde rejects, afwijkend saldo, onverklaarde open orders of een rate-limitreactie worden **nieuwe BUY's** geblokkeerd tot de reconciliatie slaagt; exits blijven toegestaan.
- **Marktschok:** bij een BTC-daling groter dan `btc_shock_bps` binnen `btc_shock_window_s` worden nieuwe BUY's gepauzeerd en bestaande posities strakker bewaakt.
- **Kill-switch:** een bestand `KILL` in de projectmap (of `kill_switch = true`) stopt alle nieuwe orders en annuleert open orders.
- **Demo/live:** standaard demo. Live vereist een expliciete configuratiewissel én een aparte key (§12).

---

## 8. Communicatie met Bitget en rate limits

### 8.1 Gekozen aanpak

| Doel | Keuze |
|---|---|
| Realtime marktdata | **Publieke WebSocket** `wss://ws.bitget.com/v2/ws/public`: `ticker`, `trade`, `books1` en `books5` voor het actieve universum. Eigen candles (1 s, 5 s, 1 min) worden lokaal uit `trade` opgebouwd |
| Orders en accountstatus | **Private WebSocket** `.../v2/ws/private`: `orders`- en `account`-kanaal voor order- en saldo-events, en orderplaatsing via de WebSocket-operatie `trade` met kanaal `place-order` |
| Opstart, metadata, tarieven, herstel | **REST**: `/spot/public/symbols`, `/common/trade-rate`, `/public/time`, saldo en open orders, `cancel-symbol-order`, en `market/candles` (1.000 candles per aanvraag, met `endTime` minstens 20 dagen terug) voor trainingsdata |

**Waarom.** Marktdata en orderevents komen als push binnen, zonder polling. Orderplaatsing via de open, geauthenticeerde WebSocket vermijdt extra handtekeningen en verbindingen per order. REST blijft voor opstart, herstel en een gecontroleerde noodroute.

**Waarom niet sneller.** `books1` en `books5` zijn snapshot-kanalen (gemeten: ~2 en ~3–5 berichten per seconde per instrument). Bij een horizon van minuten is tick-by-tick data niet nodig.

**Authenticatie en omgevingen.**
- Handtekening: HMAC-SHA256 over `timestamp + methode + pad (+ query) + body`, base64, met de headers `ACCESS-KEY`, `ACCESS-SIGN`, `ACCESS-TIMESTAMP` en `ACCESS-PASSPHRASE`. Voor de WebSocket-login is het te ondertekenen pad `GET/user/verify` met timestamp in seconden.
- **Klokoffset:** de klok van de ontwikkelpc liep ~5 s achter op de server. Bij opstart wordt `GET /public/time` gemeten en de offset bij elke handtekening toegepast.
- **Demo:** dezelfde REST-host `api.bitget.com` met de extra header `paptrading: 1` en een **demo-key**. Private WebSocket demo: `wss://wspap.bitget.com/v2/ws/private`. De demo-key werkt niet op de live host (foutcode `30017`, "Current environment does not match the API Key"). Het demo-account is apart gemaakt en kreeg 5.000 USDT.
- **Live keys:** de bot heeft voor data **geen** key nodig. Het accounttarief wordt gelezen met een aparte **alleen-lezen** live key (`BITGET_*_LIVE_READ`, aanwezig). Voor echte orders is een tweede live key (`BITGET_*_LIVE`) nodig met alleen `spot trade`, zonder withdraw en met IP-whitelist (§12); die bestaat nog niet.

**Orderpad (op demo geverifieerd).**
- Plaatsen: REST `POST /spot/trade/place-order` (~297 ms round-trip, engine ~87 ms) en WebSocket `op: trade`, `channel: place-order` (bevestiging ~288 ms, het `orders`-kanaal pusht direct de nieuwe order).
- `force`: `gtc` en `ioc` werken. Een marketable IOC wordt gevuld tegen de beste beschikbare prijs binnen de limiet; een niet-marketable IOC krijgt status `cancelled`.
- Annuleren: `cancel-symbol-order` ruimt alle open orders van een symbool op.
- **Fee:** wordt geheven in de **ontvangen valuta**. Bij een BUY van 0,0079 ETH werd 0,0000079 ETH (0,10%) ingehouden en bleef 0,0078921 ETH over; bij een SELL gaat de fee af van de ontvangen USDT.

**Demo is beperkt en niet representatief.**
- Slechts 27 symbolen, 15 online, waarvan ongeveer 8 echte crypto: `BTC`, `ETH`, `DOGE`, `XLM`, `CRV`, `SOL`, `BGB`, plus USDC-paren. **Geen enkele van de kernparen (WLD, NEAR, SUI, INJ, ONDO, …) staat online op demo.**
- Prijzen en spreads wijken af van live: `SOL` ~6.200 bps van de livekoers en 1.317 bps spread, `BGB` ~4.900 bps, `UNI` ~8.700 bps; `BTC` en `ETH` zijn bruikbaar (spread ~1 bps).
- Daarom bewijst demo de werking van de orderketen, **niet** de winstgevendheid of de vulkwaliteit. Dat is een bewuste keuze: beslissingen op live-data, uitvoering op demo voor de keten, en een **lokale fill-simulator tegen de live orderboeken** voor de echte beoordeling (§13).

**Latency.** ~270 ms REST round-trip vanaf een gewone pc (Cloudflare, Brussel). Voor een horizon van minuten is dat acceptabel; de slippage wordt gemeten tegen die vertraging (§8.4).

### 8.2 Rate limits

| Limiet | Waarde |
|---|---|
| REST-headers | Elke respons bevat `x-mbx-used-remain-limit` (waarden 3–19; daalt onder hoge belasting en bereikt 3 vlak voor de 429's, dus het is het resterende budget) |
| Publieke REST | **Gemeten 11 okt 2026 (vanuit Brussel):** foutvrij tot 20 aanvragen/s; bij 40/s 35 van 120 aanvragen met HTTP 429 |
| Orders (REST, demo) | **Gemeten:** foutvrij tot 10 orders/s (30 van 30); bij 20/s 1 van 60 met 429. De p95-latency stijgt dan van ~310 naar ~510 ms |
| WebSocket (publiek) | **Gemeten:** 120 abonnementen op één verbinding zonder fout; 3,8 `books5`-berichten/s, 3,1 `ticker`/s en 0,8 `trade`/s per paar voor de liquidste paren |
| Latency | REST en WS-orders `order → ack` 250–300 ms (p50 ~270 ms); eerste fill ~210 ms na aanmaken |

De limieten komen uit eigen metingen (`python -m tradingbot measure`); de Bitget-documentatie was niet raadpleegbaar. De bot gebruikt **50% van het foutvrije maximum** als budget (`[rate]` in `config.toml`: 10 publieke aanvragen/s, 5 orders/s). Niet gemeten: de limiet van private GET-aanvragen en het berichtenplafond van de private WebSocket (budget 8/s, conservatief), en de limieten op live; die kunnen afwijken van demo.

### 8.3 Waar zit de echte bottleneck?

Met tot ~20 instrumenten en posities van tientallen minuten zijn dat hooguit enkele orders per minuut. Waarschijnlijk is de order-rate-limiet niet de bottleneck. De echte beperkingen zijn:

1. **Kosten en edge** (§2): de bepalende grens.
2. **Dunne orderboeken** (§3.3): ordergrootte en slippage.
3. **Het kleine universum** (§3.4).
4. **Dataverwerking:** `books5` en `trade` voor ~20 paren geven tientallen berichten per seconde; coalesceren per instrument is nodig.
5. **Gedeelde USDT:** posities zijn onafhankelijk, de USDT-pot is dat niet; de allocator bepaalt wie het mag uitgeven.

### 8.4 De limieten zelf toetsen (verplichte meting)

1. Bevestig de order- en WebSocket-limieten in de documentatie, en meet ze op demo met een client zonder verbindingsplafond (meerdere gelijktijdige verbindingen of een asynchrone HTTP-client): bouw de belasting op (10, 20, 40, 80 orders per seconde) en noteer de foutcode en het herstel bij overschrijding.
2. Meet `order → ack`, `order → fill` en cancel-latency (p50/p95/p99), via REST en via de WebSocket.
3. Meet berichtsnelheid per kanaal en per instrument, en hoeveel instrumenten één verbinding aankan zonder achterstand.
4. Meet hoeveel subscribe-acties per uur het universum nodig heeft.

De gemeten waarden komen als `rate_budget_*` in de config; de bot gebruikt die, niet de maxima. Omdat demo-limieten kunnen afwijken van live, volgt een voorzichtige meting op live met minimale ordergrootte vóór echte handel (§13).

### 8.5 Slimme aanpak voor verzamelen, beslissen en handelen

- **Event-gedreven en in het geheugen:** geen polling. Per instrument één rollende state (candles, laatste trades, orderboek top-5, features) die per bericht wordt bijgewerkt.
- **Coalescing:** bij achterstand wordt de nieuwste consistente state verwerkt, niet elk gemist bericht. Maximaal één besluit per instrument per `decision_interval_ms` (standaard 500).
- **Gelaagde aandacht:** het brede universum via goedkope `ticker`; zware kanalen (`books5`) alleen voor het actieve universum.
- **Gespreide verzoeken:** één centrale scheduler met token buckets per instrument en globaal, ingesteld onder de gemeten waarden; reserveerruimte voor cancels en exits; **exits hebben voorrang op entries**; bij een limietfout backoff met jitter, geen retry-storm.
- **Vooraf berekend:** tick- en lotafronding, prijsband (±2%), break-even en maximale hoeveelheid per instrument worden voorbereid, zodat de orderroute alleen nog invult en verstuurt.
- **Eén eigenaar van de orderstatus:** de private `orders`-stream is leidend; REST is reconciliatie. Een orderacceptatie is geen fillbevestiging.
- **Terugvallen:** valt de private WebSocket weg, dan eerst orderstatus via REST nalopen en pas daarna (indien nodig) via REST handelen, met dezelfde `clientOid`.

---

## 9. Onafhankelijke posities en boekhouding

- Positie-identiteit is het instrument (`BASE-USDT`, bv. `WLDUSDT`). Per positie: hoeveelheid, gemiddelde instapprijs (uit fills), betaalde fees, hoogste prijs sinds instap, open orders, realized en unrealized P&L en de agentversies die de beslissing droegen.
- **Fee in de ontvangen valuta.** Bij een **BUY** gaat de fee af van de gekochte crypto, bij een **SELL** van de ontvangen USDT. De beschikbare hoeveelheid om te verkopen is dus **fill min fee**, niet de gevraagde hoeveelheid. De boekhouding rekent per fill met de fee en de feemunt (`feeDetail`), en een SELL gebruikt de saldo-informatie uit het `account`-kanaal om niet meer te verkopen dan er is.
- **Cash:** USDT is gedeeld. De allocator reserveert per BUY, geeft vrij bij annulering of fill, en laat nooit meer reserveren dan beschikbaar min `min_usdt_reserve`. Opbrengst van een verkoop wordt niet automatisch aan een andere positie toegewezen.
- **P&L enkel uit fills:** nooit uit orderacceptaties.
- **Reconciliatie:** bij opstart, na elke reconnect en periodiek vergelijkt de bot zijn ledger met de Bitget-saldi en open orders. Vooraf aanwezige holdings die niet door de bot zijn geopend worden **niet** als botpositie beschouwd en nooit verkocht. Bij een onverklaard verschil: BUY's pauzeren tot het opgelost is.

---

## 10. Leren en feedback

### 10.1 Hoe een uitkomst beoordelen

Altijd **netto na kosten** en met **uitvoerbare prijzen**:

```
netto_rendement_bps = (prijs_exit / prijs_entry − 1) × 10000 − rondrit_kosten_bps
```

- Instap = beste ask, exit = beste bid (of de take-profit/stop-prijs bij een barrier-uitkomst), niet de mid-prijs.
- Een BUY is juist als het netto rendement > 0 is; een HOLD is juist als geen enkele instap winstgevend was.
- Elk advies wordt op **alle horizons** (10, 30, 60, 120 min) beoordeeld en gelogd (`outcomes`); voor leren en lifecycle telt de horizon van het advies.
- Secundair signaal: de **maximale adverse beweging** om stops te kalibreren.

### 10.2 Kalibratie van de kans

`P_win` moet kloppen: een kans van 0,6 hoort in ~60% van de gevallen uit te komen, anders klopt de EV-formule (§6.2) niet. Kalibreer per modelversie en horizon (isotone regressie of Platt-schaling op een aparte kalibratieset), controleer met Brier score en betrouwbaarheidsdiagram, en herijk periodiek. Voor de regelagenten kalibreert een emmersysteem (0,1-breed, minstens 30 voorbeelden per emmer) hun ratio.

### 10.3 Data en training

- **Triple-barrier labels.** Per instap-moment en horizon `H`: `TP = max(tp_cost_multiple × kosten, 1,0 × σ_H)` boven de instapprijs, `SL = sl_sigma_multiple × σ_H` eronder, en een tijdslimiet `H`. Label = welke barrière eerst geraakt wordt (`win`, `loss`, `time`), met kosten verrekend. Instap-momenten worden zo gekozen dat overlappende labels de validatie niet lekken (purged en embargoed splits).
- **Beginnen zonder wachten.** Het eerste model wordt getraind op **historische 1-minuutcandles via REST** (`market/candles`, 16 dagen, 23.041 bars per paar, ~370.000 bars voor 16 paren inclusief BTC en ETH als marktcontext). Er zijn geen historische trades of orderboeken gebruikt; die features komen via de recorder in versie 2.
- **Beperkte hoeveelheid data.** Een universum van 9 tot 20 paren geeft per dag maar enkele tientallen onafhankelijke instap-momenten. Het model traint daarom gepoold op alle liquide USDT-paren (niet alleen de kern), met de kosten en spread per doelinstrument toegepast.
- **Versie 2 met orderboekfeatures.** Er is geen publieke historische orderboekdata. Een **recorder** (fase 1) slaat daarom `trade` en `books5` op voor het actieve universum; zodra er genoeg is, volgt een tweede model met die features, dat pas actief wordt als het het eerste verslaat.
- **Splits en validatie:** chronologisch 60% train, 20% validatie (early stopping), 20% ongebruikte test, met een buffer ≥ de langste horizon tussen de blokken en walk-forward evaluatie; nooit willekeurig schudden.

### 10.4 Validatiepoort en lifecycle

- **Poort voor stemmen.** Op de ongebruikte testperiode moet de beslisregel netto na kosten een positief gemiddeld resultaat halen met minstens `min_test_signals` signalen; de netto hit-rate moet significant boven toeval op dezelfde signalen liggen; de gerealiseerde IC moet minstens de benodigde waarde uit §2.2 halen; en het model moet de basislijn `AGENT_ORDER_FLOW` verslaan en een eenvoudige referentie ("koop bij volumepiek en verkoop na H") ook.
- **Shadow.** Haalt een model de poort niet, dan draait het in *shadow*: adviezen worden gelogd en beoordeeld, maar het stemt niet mee. Het wordt actief na 100 beoordeelde directionele adviezen in de live feed met netto hit-rate ≥ 52% en positief gemiddeld resultaat, en keert terug naar shadow na 200 adviezen met hit-rate < 45% of negatief gemiddeld resultaat. Een nieuwe modelversie begint opnieuw. Met 5–20 trades per dag duurt dit weken; dat is een eigenschap van de aanpak.
- **Hertraining:** buiten de snelle lus, bv. dagelijks of wekelijks op een rollend venster, met atomische vervanging van het modelbestand. Een gevalideerd model wordt nooit vervangen door een model dat de poort niet haalt.
- **Replay voor aanpassing van drempels.** Drempels (`min_ev_bps`, `min_p_win`, `tp_cost_multiple`, …) wijzigen alleen na een replay over de gelogde adviesmomenten, met uitvoering op de volgende beschikbare prijs, taker-fills tegen de opgenomen orderboeken en de gemeten latency. Nieuwe waarden moeten tegelijk een hoger netto rendement, een niet grotere drawdown dan 1,2× het huidige beleid en een gelijke of betere hit-rate halen.

### 10.5 Valkuilen die we vermijden

- **Look-ahead bias:** labels en features gebruiken alleen data tot het adviesmoment; de labelhorizon wordt buiten het trainingsvenster gehouden.
- **Onrealistische fills:** een backtest die op mid-prijs vult overschat het resultaat sterk. Alleen taker-fills tegen bied/laat, met latency en beperkte diepte. Dat geldt extra voor de dunne paren (NEAR, ONDO, ADA, PUMP).
- **Dunne paren en prijsvervuiling:** nieuwe en obscure listings kunnen gemanipuleerd zijn (§3.4). Alleen paren met langdurige, stabiele metingen komen in het universum.
- **Gecorreleerde posities:** negen altcoins die samen met BTC vallen zijn één positie, geen negen; de blootstellingslimiet (§7) vangt dit op.
- **Niet-stationariteit:** de dagelijkse volatiliteit schommelt sterk (§2.2); daarom rollende hertraining, lifecycle en dagelijkse herijking van het universum.
- **Backtest versus training:** een backtest slaat modellen over die op (een deel van) de testperiode getraind zijn.

---

## 11. Technologiekeuze, componenten en logs

**Taal: Python 3.11+**, met `asyncio` voor I/O.

| Doel | Keuze |
|---|---|
| WebSocket en REST | `websockets` en `aiohttp` met een eigen dunne Bitget-client (HMAC-SHA256, klokoffset, verbindingspool zonder 2-verbindingen-limiet) zodat orderpad en foutafhandeling onder eigen controle staan |
| Model | LightGBM (CPU) |
| Data | numpy en pandas voor training en analyse; ringbuffers (`collections.deque`/numpy) in de snelle lus |
| Opslag | SQLite voor orders, fills, beslissingen en outcomes; Parquet-bestanden voor opgenomen en historische marktdata |
| Backtest/replay | eigen event-replay met dezelfde agent- en risk-code als live |
| Configuratie | `config.toml`; geheimen alleen uit `.env` |

Met een horizon van minuten is Python ruim snel genoeg. Blijkt uit de meting (§8.4) dat de lus-latency toch een probleem is, dan wordt alleen dat onderdeel herzien.

### Mapstructuur (zoals gebouwd)

```text
tradingbot/
  config.py           # config.toml + .env, validatie, demo/live-combinatie
  exchange/           # base.py (types + TradingGateway), bitget/ (rest, ws_public, ws_private, gateway, signing)
  ratelimit.py        # token buckets en backoff met jitter
  marketdata.py       # bars uit trades, orderboek, ticker, flow, freshness
  features.py         # candlefeatures (identiek voor training en live) en barrières
  costs.py            # kosten van een rondrit
  recorder.py         # opname van trade/books5/ticker (gzip-jsonl) voor replay
  history.py          # historische candles via REST naar Parquet
  agents/             # universe, order_flow, edge_lgbm, regime, position_exit, base
  decision.py         # beslissingsagent (EV-regel)
  risk.py             # harde regels, sizing, kill-switch
  execution.py        # orderstatusmachine, prioriteit voor exits, take-profit, reconciliatie
  portfolio.py        # posities, USDT-allocator, P&L uit fills, drawdown
  feedback.py         # outcomes, kalibratie, lifecycle
  model.py, training.py  # LightGBM + isotone kalibratie, triple-barrier, splits, validatiepoort
  fillsim.py          # SimGateway: lokale fill-simulator achter dezelfde interface als Bitget
  backtest.py         # event-replay van opnames door dezelfde botcode
  measure.py          # limietmetingen (alleen demo)
  bot.py, app.py, cli.py  # orkestratie, opbouw, commando's
  storage.py, health.py
config.toml, .env     # drempels en geheimen
models/, data/, reports/   # modellen, SQLite/opnames/historie, rapporten (niet in git)
tests/                # 128 tests
```

### Documentatie-overzicht: parameters en outputs per component

| Component | Input | Output |
|---|---|---|
| `AGENT_UNIVERSE` | Symbolenlijst, tickers, candles, orderboeken | `active_instruments` |
| `AGENT_ORDER_FLOW` | `trade`, `books5`, `ticker` | `advice`, `ratio`, `horizon_seconds`, `expected_net_edge_bps` |
| `AGENT_EDGE_LGBM` | Features per instrument | idem, per horizon, plus `tp_bps`, `sl_bps`, `P_win` |
| `AGENT_REGIME_FILTER` | Spread, diepte, volatiliteit, BTC-regime | `regime` per instrument |
| `AGENT_POSITION_EXIT` | Eigen positie + marktstate | `HOLD` / `SELL_PARTIAL` / `SELL_ALL` + reden |
| Beslissingsagent | Adviezen, regime, positie, USDT, diepte, fees | `action`, `notional_usdt`, `order_style`, `ev_bps`, `agent_contributions` |
| Risk & Rules Layer | Voorstel + portefeuille + limieten | Goedgekeurd / afgekeurd / gecorrigeerd order + reden |
| Orderuitvoering | Goedgekeurd order | Order via WebSocket, status uit events, fills |
| Feedback-module | Advies-log + latere prijzen | Outcomes per horizon, kalibratie, lifecycle-status |

### Logtabellen (minimaal)

- `advices`: elk advies met features-snapshot en modelversie
- `outcomes`: uitkomst per advies per horizon
- `decisions`: elke beslissing met bijdragen per agent en risk-uitkomst
- `orders`: `clientOid`, Bitget-order-id, gevraagde en gevulde hoeveelheid, gemiddelde vulprijs, fee en feemunt, status, `order → ack`- en `order → fill`-latency
- `positions`: stand per instrument over tijd
- `agent_stats`: rollende hit-rate, kalibratie en lifecycle per agent
- `rate_events`: elke limietreactie, backoff en gemeten verbruik

**Metrics (altijd gelogd):** berichtsnelheid en achterstand per kanaal, lus-latency, inferentielatency, `order → ack`, `order → fill`, cancel-latency, rate-limitfouten, gemiste signalen, fill-ratio, gerealiseerde slippage versus verwachting, en afwijking tussen ledger en Bitget-saldo.

---

## 12. Configuratie, geheimen en omgevingen

- **Geheimen:** de `.env` bevat per omgeving een key-id, secret en passphrase: `BITGET_API_KEY_ID_DEMO`, `BITGET_API_SECRET_KEY_DEMO`, `BITGET_API_PASSPHRASE_2_DEMO`, dezelfde drie met `_LIVE_READ` (alleen lezen) en, pas voor echte handel, met `_LIVE`. Nooit loggen of committen: keys, secrets, passphrases, handtekeningen of auth-headers. `.env` staat in `.gitignore`; de configuratie toont credentials nooit in `repr`.
- **Live handelssleutel nog te maken.** De alleen-lezen live key is aangemaakt. Voor echte handel komt er een aparte sleutel met alleen `spot trade` (geen withdraw, geen margin of contracten) en een IP-whitelist; zonder die sleutel kan de bot op live niet handelen.
- **Domeinen:** één REST-host `api.bitget.com`; demo via de header `paptrading: 1`. WebSocket: `ws.bitget.com` (live en publiek) en `wspap.bitget.com` (demo privaat). Een niet-passende combinatie (demo-key tegen live host of omgekeerd) stopt de bot bij opstart.
- **Demo versus live:** apart gecodeerd (`environment = "demo" | "live"`); live is standaard uit en vereist een expliciete wissel.
- **Config-secties** (`config.toml`): `[exchange]`, `[universe]`, `[agents]`, `[decision]`, `[fees]`, `[risk]`, `[rate]`, `[model]`, `[recorder]`, `[health]`. Alle drempels uit dit document staan daar en zijn leidend.
- **Startgedrag:** ontbreekt een verplichte instelling, of mislukt de klok-, tarief-, symbool- of saldo-controle, dan start de bot niet of start hij zonder te kopen.

---

## 13. Aanpak in fasen

| Fase | Inhoud | Klaar als |
|---|---|---|
| 1. Infrastructuur en meetmodus | Bitget-client (WS + REST), marktdata-state, recorder, `AGENT_UNIVERSE`, historische data-downloader, metingen uit §8.4, fee- en saldo-controle | Stabiele feeds, reconnect getest, limieten, latency en fee-tarief gemeten en vastgelegd |
| 2. Onderzoek (go/no-go) | Triple-barrier labels op 16+ dagen historische candles en trades; basislijn `AGENT_ORDER_FLOW`; per paar en horizon de gerealiseerde IC en het netto resultaat | **Netto-edge na kosten aangetoond op ongebruikte data (§10.4), of het project stopt of verlegt zijn horizon/universum** |
| 3. Model en backtest | `AGENT_EDGE_LGBM`, kalibratie, validatiepoort, event-replay met latency en taker-fills tegen opgenomen orderboeken | Model haalt de poort en verslaat de basislijn netto |
| 4. Beslissing, risk, uitvoering | Beslissingsagent, `risk.py`, scheduler, orderstatusmachine, boekhouding, reconciliatie | Testsuite (hieronder) groen |
| 5. Shadow op live data | Hele keten live op de echte feed zonder orders; lokale fill-simulator tegen de live orderboeken (taker tegen bied/laat plus slippage naar diepte) | Shadow-resultaat netto positief over minstens enkele weken en meerdere marktregimes |
| 6. Demo-orders | Echte orders op Bitget demo, laag tempo, alleen demo-bruikbare paren (`BTC`, `ETH`, `DOGE`, `XLM`, `CRV`); test van REST- en WebSocket-orders, IOC, annuleren, fees in ontvangen valuta | Orderketen betrouwbaar: geen reconciliatiefouten, rate-limitgedrag zoals gemeten. **Zegt niets over winstgevendheid of vulkwaliteit voor de kernparen** |
| 7. Klein echt kapitaal | Alleen na expliciete beslissing; aparte live key (alleen `spot trade`, IP-whitelist, geen withdraw); eerst een minimale testorder om te bevestigen dat de kernparen handelbaar zijn voor het account; alle limieten actief; tweede limietmeting op live | Niet gestart |

**Status per fase (11 okt 2026):** 1 klaar (feeds, recorder, history, metingen); 2 uitgevoerd met **negatief resultaat** (geen edge, §15); 3 gebouwd (model, kalibratie, poort, replay), poort niet gehaald; 4 klaar (128 tests groen); 5 loopt (shadow-run 3 minuten live getest, lange run en opnames nog nodig); 6 gedaan voor de keten (WS-order, IOC, fee in basismunt, reconciliatie op demo); 7 niet gestart.

**Verplichte tests vóór fase 6:** order-statusmachine (acceptatie, gedeeltelijke fill, fill, cancel, `cancelled` bij IOC, reject, dubbele events), idempotentie op `clientOid`, reconnect en reconciliatie, USDT-reservering bij gelijktijdige BUY's, afronding naar prijs- en hoeveelheidsprecisie en minimum, fee in ontvangen versus quote-valuta en de netto verkoophoeveelheid, scheduler-voorrang van exits, backoff bij limietfouten, circuit breaker en kill-switch, take-profit/stop-loss-logica, prijsband ±2%, en een replaytest die dezelfde uitkomst geeft als live.

---

## 14. Vastgelegde keuzes en startwaarden

- **Beurs en cash:** Bitget, spot, USDT.
- **Universum:** kern `NEAR`, `WLD`, `INJ`, `SUI`, `ZEC`, `ONDO`, `PUMP`, `UNI`, `ADA` (alle `-USDT`), daarna dynamisch (§3.2); alle andere paren, ook `HYPE`, `DOGE`, `SOL`, `XRP`, `BTC` en `ETH`, doen mee zodra ze de criteria halen en de EV-regel een signaal geeft; `BTC` en `ETH` zijn bovendien altijd marktcontext.
- **Marktdatakanalen:** `ticker`, `trade`, `books1`, `books5`.
- **Horizonnen model:** 10, 30, 60 en 120 min.
- **Orderstijl:** instap en stop-loss als limit IOC; take-profit als wachtende limietorder; orders via de private WebSocket.
- **Beslisregel:** EV na kosten (§6.2).
- **Startwaarden (te kalibreren in fase 1 en 2):** `base_order_usdt` klein (orde 20–100 USDT), `max_open_positions = 6`, `min_ev_bps = 5`, `min_p_win = 0,45`, `tp_cost_multiple = 2,0`, `sl_sigma_multiple = 1,0`, `max_data_age_ms = 2000`, `decision_interval_ms = 500`, `min_volume_24h_usdt = 2e6`, `max_spread_bps = 8`, `min_sd1m_bps = 5`, `min_depth_usdt = 5000`, `max_order_depth_fraction = 0,10`, `top_n = 20`.
- **Te controleren vóór de start:** het werkelijke fee-tarief per paar (`GET /common/trade-rate`), of de kernparen handelbaar zijn voor het live-account, de limieten uit §8.4 op demo én live, de gemeten latency vanaf de plek waar de bot draait, en of de gemeten edge in fase 2 standhoudt.

---

## 15. Open punten

1. **Bestaat er een netto edge?** Dit is de hoofdvraag. **Eerste antwoord (11 okt 2026, 16 dagen, 16 paren): nee.** Het model leert vooral volatiliteit (correlatie 0,91 tussen voorspelde kans en `vol_30` op 10 min) en nauwelijks richting. De beste verwachte waarde op de ongebruikte testperiode is −2,6 bps (60 min) en geen enkel testsignaal haalt `min_ev_bps`; de referentieregels verliezen gemiddeld 22–24 bps per trade (≈ de kosten). Het model draait daarom alleen in shadow en de bot plaatst geen orders. Meer data (de recorder), orderboekfeatures (versie 2) en een langere periode zijn de volgende stappen; zonder bewijs wordt niet gehandeld. **Tweede antwoord (walk-forward, 150 dagen, 18 paren, 22 wekelijkse hertrainingen): nog steeds geen aantoonbare edge.** De poort slaagde in 0 van 22 weken. Alle EV-signalen samen gaven 293 trades met gemiddeld −1,1 bps netto (t = −0,18, rendement −0,05%). De twee lifecycle-varianten zijn positief (+0,34% en +0,59%), maar dat komt vrijwel volledig van één instrument, `UPCUSDT` (24 van 32 trades, gemiddeld +95 bps), een jong en vermoedelijk dun paar waarvan candle-fills onrealistisch zijn; zonder dat paar blijft er niets over. De gemiddelde voorspelde EV (52,7 bps) ligt ver boven de gerealiseerde (10,7 bps), dus het model is overmoedig, en het weekresultaat schommelt tussen −78 en +138 bps (regimeafhankelijk).
2. **Wettelijke toelaatbaarheid van Bitget voor de gebruiker.** Er is aangenomen dat Bitget mag. Dit is niet gecontroleerd; publieke bronnen zeggen dat Bitget in 2026 geen nieuwe EU-klanten kan onboarden zolang de MiCA-vergunning wacht. Het bestaan van het account en van een live key spreekt dat tegen, maar bevestigt de juridische status niet. Controleer dit en de fiscale gevolgen voordat er echt kapitaal in gaat.
3. **Handelbaarheid van de kernparen voor het live-account.** De publieke symbolenlijst is globaal; alleen een minimale live testorder bewijst per paar dat het account erop mag handelen.
4. **Limieten op live en private limieten** zijn niet gemeten (§8.2). Publieke REST en orders zijn op demo gemeten.
5. **Dunne diepte (§3.3).** Orders worden begrensd tot een fractie van de diepte; bij groter kapitaal moet de omvang of het universum aangepast worden.
6. **Maker-voordeel ontbreekt.** Maker en taker kosten allebei 0,10%. Een mogelijke korting (bv. via BGB) en het effect van VIP-niveaus zijn niet onderzocht.
7. **Hosting:** waar draait de bot? Dat beïnvloedt latency en slippage, minder de haalbaarheid.
8. **Demo is niet representatief:** de kernparen staan niet online op demo en de demo-prijzen en -spreads wijken af. Demo bewijst de werking van de orderketen; de echte uitvoerbaarheid wordt gemeten in shadow op live-data en daarna met minimaal kapitaal.
9. **Shorten** zou het aantal kansen verdubbelen, maar vereist derivaten en valt buiten dit ontwerp.

---

## 16. Implementatie en gebruik

Alle commando's draaien met `.\.venv\Scripts\python -m tradingbot <commando>` (Python 3.13, afhankelijkheden in `requirements.txt`).

| Commando | Doel |
|---|---|
| `check` | Klokoffset, publieke data, demo-auth, private WS-login en (alleen-lezen) live-accounttarief |
| `history --days 16` | Download 1-minuutcandles voor de liquide USDT-paren naar `data/history` |
| `train` | Triple-barrier labels, LightGBM per horizon, kalibratie, validatiepoort; rapport in `reports/` |
| `run [--minutes N]` | De bot. `mode = "shadow"` gebruikt de fill-simulator, `mode = "trade"` echte orders op de gekozen omgeving |
| `replay` | Opgenomen marktdata (`data/recordings`) door dezelfde botcode met de fill-simulator |
| `simulate` | Walk-forward simulatie "alsof live" op de historie: wekelijks hertrainen op het verleden (60 dagen, poort op de laatste 20%), daarna de volgende week minuut voor minuut met dezelfde EV-regel, risicolimieten en lifecycle; drie varianten (alle signalen, lifecycle strikt, lifecycle doorlopend); slaat het eindmodel op |
| `simulate` | Walk-forward simulatie "alsof live" over de gedownloade historie (zie hieronder) |
| `measure [--ws-only] [--skip-orders]` | Limietmeting; weigert op live, orders alleen op demo |
| `demo-smoke` | E\u00e9n kleine BTC-koop en -verkoop op demo via de uitvoeringslaag |
| `status` | Toont `data/health.json` |

**Veiligheidsmechanismen in de code.**
- Standaard `shadow` en `demo`. `mode = "trade"` op live faalt zonder `live_confirmed = true`, en `force_model_active` is daar verboden.
- Een nieuw of afgekeurd model draait in shadow en stemt pas mee na het live bewijs uit \u00a710.4 (`edge_status` in `health.json`).
- De bot plaatst geen order zonder dat `check_entry` van de Risk-laag akkoord geeft; exits blijven altijd mogelijk, ook bij kill-switch (`KILL`-bestand) en circuit breaker.
- Orders waarvan de uitkomst onbekend is (time-out na verzenden) worden nooit blind herhaald, maar via `clientOid` opgevraagd (\u00a77, punt 6).
- Een instrument dat uit het universum valt terwijl er een positie open staat gaat naar `EXIT_ONLY` (\u00a73.2).
- Bij stoppen van de bot in trade-modus blijven wachtende take-profit-orders staan; open posities worden dan niet meer bewaakt.

**Ontdekt tijdens het bouwen** (nu in de code): de WS-orderoperatie vereist `instType: "SPOT"` in hoofdletters (kleine letters geven foutcode 41101); `market/history-candles` geeft maximaal 200 candles, `market/candles` 1.000; het symbolenoverzicht bevat `openTime` (noodzakelijk voor `min_listing_age`); en een verkoop na een aankoop moet de hoeveelheid minus de fee gebruiken (op demo: 0,00012 BTC gekocht, 0,00011988 over). De round-trip op demo kostte 0,0208 USDT op 10 USDT, dus ~21 bps.
