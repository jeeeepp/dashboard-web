#!/usr/bin/env python3
"""Quick manual smoke test for conditions.py using synthetic price data (no
network) -- NOT part of the deployed app, just a local verification script.
Delete or convert to pytest later if this project grows a real test suite.
"""
from conditions import ScanFilterRequest, RsiCondition, MaGroupCondition, evaluate_ticker

def check(label, condition):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    assert condition, label

# --- Steadily rising price series: 300 days, linear uptrend 100 -> 250 ----
rising = [100 + i * 0.5 for i in range(300)]
price = rising[-1]  # 249.5

# 1) SMA enabled, price > SMA(5,20,150,200) on a strong uptrend -> should match
req = ScanFilterRequest(sma=MaGroupCondition(enabled=True, operator=">", periods=[5, 20, 150, 200]))
result = evaluate_ticker(rising, price, req)
check("uptrend: price > SMA(5,20,150,200) matches", result is not None and result["match"] is True)
check("uptrend: sma dict has 4 periods", len(result["sma"]) == 4)

# 2) Same series, but require price < SMA(5) -> should NOT match (price is above SMA5 in an uptrend)
req2 = ScanFilterRequest(sma=MaGroupCondition(enabled=True, operator="<", periods=[5]))
result2 = evaluate_ticker(rising, price, req2)
check("uptrend: price < SMA(5) does NOT match", result2 is not None and result2["match"] is False)

# 3) EMA enabled, price > EMA(7,30,89,200) on uptrend -> should match
req3 = ScanFilterRequest(ema=MaGroupCondition(enabled=True, operator=">", periods=[7, 30, 89, 200]))
result3 = evaluate_ticker(rising, price, req3)
check("uptrend: price > EMA(7,30,89,200) matches", result3 is not None and result3["match"] is True)

# 4) RSI enabled on a monotonic uptrend -> RSI should be high (>50, likely near 100 for pure uptrend with a long enough period so it's fully warmed up)
req4 = ScanFilterRequest(rsi=RsiCondition(enabled=True, period=14, operator=">", threshold=50))
result4 = evaluate_ticker(rising, price, req4)
check("uptrend: RSI(14) > 50 matches", result4 is not None and result4["match"] is True)
check("uptrend: RSI value is a number", isinstance(result4["rsi"], float))
print(f"    RSI(14) on pure uptrend = {result4['rsi']}")

# 5) Insufficient history: period larger than available closes -> excluded (None)
short_series = [100.0] * 10
req5 = ScanFilterRequest(sma=MaGroupCondition(enabled=True, operator=">", periods=[200]))
result5 = evaluate_ticker(short_series, 100.0, req5)
check("insufficient history for SMA(200) on 10 points -> None", result5 is None)

# 6) All disabled -> match True vacuously (should show up with no computed values)
req6 = ScanFilterRequest()
result6 = evaluate_ticker(rising, price, req6)
check("all disabled -> match True (vacuous)", result6 is not None and result6["match"] is True)
check("all disabled -> rsi/sma/ema empty", result6["rsi"] is None and result6["sma"] == {} and result6["ema"] == {})

# 7) Equality operator with a sensible tolerance
flat = [150.0] * 250
req7 = ScanFilterRequest(sma=MaGroupCondition(enabled=True, operator="=", periods=[20]))
result7 = evaluate_ticker(flat, 150.0, req7)
check("flat series: price = SMA(20) matches (both exactly 150)", result7 is not None and result7["match"] is True)

print("\nAll conditions.py manual checks passed.")
