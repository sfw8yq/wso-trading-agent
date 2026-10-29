"""Trading universe: liquid US large/mid caps grouped by sector, plus the
index/sector ETFs used by the regime engine.

List A (default) = market cap >= $10B and avg dollar volume >= $50M; the
screener re-checks both numerically on every run, so this list is only the
starting set. Users can add/remove tickers in Settings.
"""

SECTOR_ETF = {
    "Technology": "XLK",
    "Semiconductors": "SOXX",
    "Communication": "XLC",
    "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP",
    "Health Care": "XLV",
    "Financials": "XLF",
    "Industrials": "XLI",
    "Energy": "XLE",
    "Materials": "XLB",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
}

# Correlated clusters count as ONE position for exposure purposes.
CLUSTERS = {
    "semis_ai_infra": ["NVDA", "AMD", "AVGO", "TSM", "MU", "LRCX", "AMAT", "KLAC", "ADI", "QCOM", "ARM", "MRVL", "SMCI", "VRT", "ANET"],
    "life_science_tools": ["TMO", "DHR", "IQV", "MEDP", "A", "WAT", "MTD", "WST", "VEEV", "RVTY"],
    "energy_producers": ["XOM", "CVX", "COP", "DVN", "EOG", "OXY", "APA", "OVV", "PR", "FANG"],
    "energy_midstream": ["LNG", "TRGP", "WMB", "KMI", "OKE"],
    "mega_cap_tech": ["AAPL", "MSFT", "GOOGL", "AMZN", "META"],
    "banks": ["JPM", "BAC", "WFC", "GS", "MS", "C"],
}

UNIVERSE = {
    "Technology": ["AAPL", "MSFT", "ORCL", "CRM", "NOW", "ADBE", "INTU", "PANW", "CRWD", "FTNT", "OKTA", "DDOG", "SNOW", "PLTR", "SHOP", "UBER", "NET", "ZS", "ANET", "CSCO", "IBM", "ACN"],
    "Semiconductors": ["NVDA", "AMD", "AVGO", "TSM", "MU", "LRCX", "AMAT", "KLAC", "ADI", "QCOM", "MRVL", "ARM"],
    "Communication": ["GOOGL", "META", "NFLX", "DIS", "SPOT", "TMUS", "T"],
    "Consumer Discretionary": ["AMZN", "TSLA", "HD", "LOW", "MCD", "NKE", "SBUX", "BKNG", "TJX", "CMG", "ORLY", "LULU", "RCL", "MAR"],
    "Consumer Staples": ["WMT", "COST", "PG", "KO", "PEP", "PM", "MDLZ", "CL", "KR"],
    "Health Care": ["LLY", "UNH", "JNJ", "ABBV", "MRK", "PFE", "AMGN", "GILD", "VRTX", "REGN", "ISRG", "BSX", "SYK", "MDT", "ABT", "TMO", "DHR", "IQV", "MEDP", "A", "WAT", "MTD", "BDX", "VEEV", "EXEL", "CI", "ELV", "HCA"],
    "Financials": ["JPM", "BAC", "WFC", "GS", "MS", "C", "BLK", "SCHW", "AXP", "V", "MA", "SPGI", "ICE", "CME", "PGR", "CB", "COIN", "HOOD"],
    "Industrials": ["CAT", "DE", "HON", "GE", "RTX", "LMT", "NOC", "GD", "BA", "UNP", "UPS", "ETN", "EMR", "PH", "WM", "CTAS", "EXPD", "URI", "TT"],
    "Energy": ["XOM", "CVX", "COP", "EOG", "DVN", "OXY", "APA", "OVV", "PR", "FANG", "SLB", "HAL", "LNG", "TRGP", "WMB", "KMI", "OKE"],
    "Materials": ["LIN", "APD", "SHW", "FCX", "NEM", "NUE", "DOW"],
    "Utilities": ["NEE", "SO", "DUK", "CEG", "VST"],
    "Real Estate": ["PLD", "AMT", "EQIX", "WELL", "O"],
}

REGIME_SYMBOLS = ["SPY", "QQQ", "IWM", "SOXX", "^VIX", "DIA"] + list(SECTOR_ETF.values())


def all_symbols(extra: list[str] | None = None, excluded: list[str] | None = None) -> list[str]:
    seen, out = set(), []
    for syms in UNIVERSE.values():
        for s in syms:
            if s not in seen:
                seen.add(s)
                out.append(s)
    for s in extra or []:
        s = s.upper().strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    ex = {s.upper() for s in (excluded or [])}
    return [s for s in out if s not in ex]


def sector_of(symbol: str) -> str:
    for sec, syms in UNIVERSE.items():
        if symbol in syms:
            return sec
    return "Other"


def cluster_of(symbol: str) -> str | None:
    for name, syms in CLUSTERS.items():
        if symbol in syms:
            return name
    return None
