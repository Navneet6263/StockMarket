import math
import scipy.stats as stats

def black_scholes_iv_and_greeks(side: str, S: float, K: float, T: float, r: float, market_price: float):
    """
    Calculate Implied Volatility (IV) and Greeks using Black-Scholes.
    side: 'CE' or 'PE'
    S: Spot Price
    K: Strike Price
    T: Time to expiry in years
    r: Risk-free rate (e.g., 0.07 for 7%)
    market_price: Current market price of the option
    """
    if S <= 0 or K <= 0 or T <= 0 or market_price <= 0:
        return {"iv": 0.0, "delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}

    # Helper: BS Price
    def bs_price(sigma):
        d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        if side == 'CE':
            return S * stats.norm.cdf(d1) - K * math.exp(-r * T) * stats.norm.cdf(d2)
        else:
            return K * math.exp(-r * T) * stats.norm.cdf(-d2) - S * stats.norm.cdf(-d1)

    # Newton-Raphson to find IV
    MAX_ITER = 100
    PRECISION = 1.0e-5
    sigma = 0.5  # initial guess
    for i in range(MAX_ITER):
        price = bs_price(sigma)
        diff = market_price - price
        if abs(diff) < PRECISION:
            break
        
        # Calculate vega for derivative
        d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
        vega = S * stats.norm.pdf(d1) * math.sqrt(T)
        
        if vega == 0.0:
            break
            
        sigma = sigma + diff / vega

    # Bound IV
    sigma = max(0.0001, min(sigma, 5.0))
    
    # Calculate Greeks with found IV
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    
    pdf_d1 = stats.norm.pdf(d1)
    cdf_d1 = stats.norm.cdf(d1)
    cdf_d2 = stats.norm.cdf(d2)
    
    if side == 'CE':
        delta = cdf_d1
        theta = (- (S * pdf_d1 * sigma) / (2 * math.sqrt(T)) 
                 - r * K * math.exp(-r * T) * cdf_d2) / 365.0
    else:
        delta = cdf_d1 - 1.0
        theta = (- (S * pdf_d1 * sigma) / (2 * math.sqrt(T)) 
                 + r * K * math.exp(-r * T) * stats.norm.cdf(-d2)) / 365.0
                 
    gamma = pdf_d1 / (S * sigma * math.sqrt(T))
    vega = S * pdf_d1 * math.sqrt(T) / 100.0

    return {
        "iv": sigma,
        "delta": delta,
        "gamma": gamma,
        "theta": theta,
        "vega": vega
    }
