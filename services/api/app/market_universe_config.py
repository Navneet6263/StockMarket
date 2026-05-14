from __future__ import annotations

from dataclasses import dataclass


NIFTY_50 = (
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK", "BAJAJ-AUTO",
    "BAJFINANCE", "BAJAJFINSV", "BEL", "BHARTIARTL", "CIPLA", "COALINDIA",
    "DRREDDY", "EICHERMOT", "ETERNAL", "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE",
    "HEROMOTOCO", "HINDALCO", "HINDUNILVR", "ICICIBANK", "INDUSINDBK", "INFY",
    "ITC", "JIOFIN", "JSWSTEEL", "KOTAKBANK", "LT", "M&M", "MARUTI", "NESTLEIND",
    "NTPC", "ONGC", "POWERGRID", "RELIANCE", "SBILIFE", "SBIN", "SHRIRAMFIN",
    "SUNPHARMA", "TATACONSUM", "TATAMOTORS", "TATASTEEL", "TCS", "TECHM", "TITAN",
    "TRENT", "ULTRACEMCO", "WIPRO",
)

NIFTY_100_EXTRA = (
    "ABB", "ADANIENSOL", "ADANIGREEN", "ADANIPOWER", "AMBUJACEM", "BAJAJHLDNG",
    "BANKBARODA", "BOSCHLTD", "BPCL", "BRITANNIA", "CANBK", "CHOLAFIN",
    "DABUR", "DIVISLAB", "DLF", "DMART", "GAIL", "GODREJCP", "HAL", "HAVELLS",
    "ICICIGI", "ICICIPRULI", "INDIGO", "IOC", "IRFC", "JINDALSTEL", "JSWENERGY",
    "LICI", "LODHA", "LTIM", "MOTHERSON", "NAUKRI", "NHPC", "PIDILITIND", "PNB",
    "RECLTD", "SHREECEM", "SIEMENS", "TATAPOWER", "TORNTPHARM", "TVSMOTOR",
    "UNIONBANK", "UNITDSPR", "VBL", "VEDL", "ZYDUSLIFE",
)

NIFTY_200_EXTRA = (
    "ABCAPITAL", "ABFRL", "ACC", "ALKEM", "APLAPOLLO", "ASHOKLEY", "ASTRAL",
    "AUBANK", "AUROPHARMA", "BALKRISIND", "BANDHANBNK", "BDL", "BHARATFORG",
    "BIOCON", "BSE", "CGPOWER", "COLPAL", "CONCOR", "CUMMINSIND", "DELHIVERY",
    "DIXON", "FEDERALBNK", "FORTIS", "GLENMARK", "GODREJPROP", "GRSE",
    "HDFCAMC", "HINDPETRO", "HINDZINC", "HUDCO", "IDFCFIRSTB", "IEX", "INDHOTEL",
    "INDIANB", "INDUSTOWER", "IRB", "IREDA", "JSWINFRA", "KPITTECH", "LICHSGFIN",
    "LTF", "LUPIN", "MANKIND", "MAXHEALTH", "MAZDOCK", "MFSL", "MPHASIS",
    "NMDC", "OBEROIRLTY", "OFSS", "OIL", "PAGEIND", "PATANJALI", "PAYTM",
    "PERSISTENT", "PETRONET", "PHOENIXLTD", "PIIND", "POLICYBZR", "POLYCAB",
    "PRESTIGE", "RVNL", "SAIL", "SBICARD", "SRF", "SUPREMEIND", "SUZLON",
    "TATACHEM", "TATAELXSI", "TATATECH", "TIINDIA", "TORNTPOWER", "UNOMINDA",
    "UPL", "YESBANK", "GREENPLY",
)

FNO_EXTRA = (
    "AARTIIND", "ABBOTINDIA", "ATGL", "BATAINDIA", "BERGEPAINT", "BSOFT",
    "CAMS", "CDSL", "CESC", "COFORGE", "CYIENT", "DALBHARAT", "EXIDEIND",
    "GRANULES", "GUJGASLTD", "HINDCOPPER", "IDEA", "IIFL", "IRCTC", "KALYANKJIL",
    "KEI", "LAURUSLABS", "MANAPPURAM", "MCX", "NATIONALUM", "NBCC", "NYKAA",
    "PFC", "POONAWALLA", "RBLBANK", "SONACOMS", "SYNGENE", "TITAGARH", "VOLTAS",
)

NIFTY_100 = tuple(dict.fromkeys((*NIFTY_50, *NIFTY_100_EXTRA)))
NIFTY_200 = tuple(dict.fromkeys((*NIFTY_100, *NIFTY_200_EXTRA)))
NIFTY_500_SEED = NIFTY_200
FNO_STOCKS = tuple(dict.fromkeys((*NIFTY_50, *FNO_EXTRA)))

# Dynamic full NSE universe cache
_ALL_NSE: tuple[str, ...] | None = None


def fetch_all_nse_symbols() -> tuple[str, ...]:
    """
    Fetch ALL NSE equity symbols from AngelOne instrument master.
    Returns 2500+ symbols. Falls back to expanded list if fetch fails.
    """
    import os
    provider = os.getenv("BROKER_PROVIDER", "yfinance").lower()
    if provider != "angelone":
        return _EXPANDED_FALLBACK
    try:
        import requests
        import certifi
        resp = requests.get(
            "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json",
            timeout=30,
            verify=certifi.where(),
        )
        if resp.status_code != 200:
            return _EXPANDED_FALLBACK
        instruments = resp.json()
        symbols = []
        for item in instruments:
            if item.get("exch_seg") != "NSE":
                continue
            symbol = item.get("symbol") or ""
            name = item.get("name") or ""
            # NSE equities have symbol ending with -EQ
            if symbol.endswith("-EQ") and name:
                clean = name.upper().strip()
                if clean and len(clean) <= 20:
                    symbols.append(clean)
        unique = tuple(dict.fromkeys(symbols))
        return unique if len(unique) > 200 else _EXPANDED_FALLBACK
    except Exception:
        return _EXPANDED_FALLBACK


# Expanded fallback = NIFTY_200 + FNO + extra active stocks
_EXPANDED_FALLBACK = tuple(dict.fromkeys((*NIFTY_100, *NIFTY_200_EXTRA, *FNO_EXTRA,
    "ZOMATO", "SWIGGY", "RVNL", "IREDA", "HUDCO", "NHPC", "IRFC", "SUZLON",
    "YESBANK", "IDEA", "NBCC", "COCHINSHIP", "MAZAGON", "BDL", "HAL", "BEL",
    "PAYTM", "NYKAA", "POLICYBZR", "DELHIVERY", "ZYDUSLIFE", "MANKIND",
    "TATATECH", "JSWINFRA", "PATANJALI", "KAYNES", "DIXON", "CDSL", "BSE",
    "CAMS", "KPITTECH", "PERSISTENT", "COFORGE", "LTIM", "MPHASIS",
    "TITAGARH", "RVNL", "IRCTC", "INDIANHOTELS", "LICI", "SBICARD",
    "MAXHEALTH", "FORTIS", "AUROPHARMA", "GRANULES", "LAURUSLABS",
    "KALYANKJIL", "TITAN", "TRENT", "DMART", "VBL", "UNITDSPR",
    "PHOENIXLTD", "PRESTIGE", "OBEROIRLTY", "GODREJPROP", "DLF", "LODHA",
    "CGPOWER", "SIEMENS", "ABB", "HAVELLS", "POLYCAB", "KEI", "VOLTAS",
    "CUMMINSIND", "THERMAX", "GRSE", "MAZDOCK", "COCHINSHIP", "TIINDIA",
    "SONACOMS", "EXIDEIND", "AMARARAJA", "MOTHERSON", "UNOMINDA",
    "SYNGENE", "BIOCON", "ALKEM", "TORNTPHARM", "IPCALAB",
    "FEDERALBNK", "RBLBANK", "AUBANK", "BANDHANBNK", "MANAPPURAM",
    "PFC", "RECLTD", "IREDA", "POONAWALLA", "CHOLAFIN", "BAJFINANCE",
    "MCX", "IEX", "ATGL", "GUJGASLTD", "PETRONET", "OIL",
    "NATIONALUM", "HINDCOPPER", "NMDC", "SAIL", "VEDL", "HINDZINC",
    "CONCOR", "INDUSTOWER", "TATACOMM", "HFCL", "STLTECH",
)))


def get_full_nse_universe() -> tuple[str, ...]:
    """Fetch full NSE universe. Re-fetches if cached value is just fallback."""
    global _ALL_NSE
    if _ALL_NSE is not None and len(_ALL_NSE) > 200:
        return _ALL_NSE
    # Always try fresh fetch
    result = fetch_all_nse_symbols()
    _ALL_NSE = result
    return _ALL_NSE


@dataclass(frozen=True)
class UniverseConfig:
    nifty_50: tuple[str, ...] = NIFTY_50
    nifty_100: tuple[str, ...] = NIFTY_100
    nifty_200: tuple[str, ...] = NIFTY_200
    nifty_500_seed: tuple[str, ...] = NIFTY_200  # fallback
    fno_stocks: tuple[str, ...] = FNO_STOCKS

    @property
    def all_nse(self) -> tuple[str, ...]:
        """Full NSE universe from AngelOne (1500+ stocks) or fallback."""
        return get_full_nse_universe()


UNIVERSE_CONFIG = UniverseConfig()
