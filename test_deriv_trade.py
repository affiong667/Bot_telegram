"""
STANDALONE DIAGNOSTIC — bypasses everything (AI, consensus, 28 instruments,
scheduler). Does ONE thing: tries to buy the simplest possible contract on
your Deriv account and prints a plain-English result.

This answers one question definitively: can this bot execute a trade on
your Deriv account at all, right now?

Run with:
    python test_deriv_trade.py

Requires only DERIV_API_TOKEN and DERIV_APP_ID (optional) in your .env —
none of the AI provider keys or Telegram keys are needed for this test.
"""

import asyncio
import json
import os
import sys

import websockets
from dotenv import load_dotenv

load_dotenv()

DERIV_APP_ID = os.getenv("DERIV_APP_ID", "1089")
DERIV_API_TOKEN = os.getenv("DERIV_API_TOKEN", "")
DERIV_WS_URL = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"

# The absolute simplest, cheapest test: a $1 stake, 5-minute Rise contract
# on EUR/USD (a symbol that's virtually always open somewhere in the forex
# trading week, and cheap enough that even a loss costs almost nothing).
TEST_SYMBOL = "frxEURUSD"
TEST_STAKE = 1.0
TEST_DURATION_SEC = 300  # 5 minutes


async def send(ws, payload):
    await ws.send(json.dumps(payload))
    raw = await ws.recv()
    return json.loads(raw)


async def main():
    print("=" * 60)
    print("DERIV STANDALONE TRADE TEST")
    print("=" * 60)

    if not DERIV_API_TOKEN:
        print("\n❌ FAILED: DERIV_API_TOKEN is not set in your .env file.")
        print("   Nothing else can be tested until this is set.")
        sys.exit(1)

    print(f"\nConnecting to {DERIV_WS_URL} ...")
    try:
        ws = await websockets.connect(DERIV_WS_URL, ping_interval=20, ping_timeout=10)
    except Exception as e:
        print(f"\n❌ FAILED at connection step: {e}")
        print("   This means we can't even reach Deriv's servers from here.")
        print("   Check your internet connection or firewall.")
        sys.exit(1)
    print("✅ Connected.")

    print("\nAuthorizing with your token...")
    auth_resp = await send(ws, {"authorize": DERIV_API_TOKEN})
    if auth_resp.get("error"):
        print(f"\n❌ FAILED at authorization: {auth_resp['error'].get('message')}")
        print("   Your DERIV_API_TOKEN is invalid, expired, or was revoked.")
        print("   Generate a fresh one at https://api.deriv.com/dashboard")
        sys.exit(1)

    account = auth_resp.get("authorize", {})
    loginid = account.get("loginid", "unknown")
    balance = account.get("balance", "unknown")
    currency = account.get("currency", "")
    is_virtual = account.get("is_virtual", None)

    print(f"✅ Authorized.")
    print(f"   Account: {loginid}")
    print(f"   Type: {'DEMO/Virtual' if is_virtual else 'REAL MONEY'}")
    print(f"   Balance: {balance} {currency}")

    if loginid.startswith("CRW") or loginid.startswith("VRW"):
        print(f"\n⚠️  WARNING: Your account ID starts with '{loginid[:3]}' — this looks")
        print("   like a WALLET account. Wallet accounts CANNOT trade via the API.")
        print("   You need a CR (real) or VRTC (demo) trading account instead.")
        print("   Continuing anyway to see what Deriv itself says...")

    print(f"\nChecking what Deriv actually allows for {TEST_SYMBOL} on your account...")
    contracts_resp = await send(ws, {"contracts_for": TEST_SYMBOL, "currency": "USD"})
    if contracts_resp.get("error"):
        print(f"   Could not fetch contract offerings: {contracts_resp['error'].get('message')}")
    else:
        available = contracts_resp.get("contracts_for", {}).get("available", [])
        rise_fall = [c for c in available if c.get("contract_category") == "callput"]
        if rise_fall:
            print(f"   Found {len(rise_fall)} Rise/Fall (callput) offering(s) for {TEST_SYMBOL}:")
            for c in rise_fall[:5]:
                print(f"     - min duration: {c.get('min_contract_duration')}, "
                      f"max duration: {c.get('max_contract_duration')}, "
                      f"barrier category: {c.get('barrier_category')}")
        else:
            categories = sorted(set(c.get("contract_category") for c in available))
            print(f"   No Rise/Fall (callput) offerings found for {TEST_SYMBOL} on this account.")
            print(f"   Available contract categories instead: {categories}")

    print(f"\nRequesting a price proposal for {TEST_SYMBOL} CALL, "
          f"${TEST_STAKE} stake, {TEST_DURATION_SEC}s duration...")
    proposal_resp = await send(ws, {
        "proposal": 1,
        "amount": TEST_STAKE,
        "basis": "stake",
        "contract_type": "CALL",
        "currency": "USD",
        "duration": TEST_DURATION_SEC,
        "duration_unit": "s",
        "symbol": TEST_SYMBOL,
    })

    if proposal_resp.get("error"):
        err = proposal_resp["error"]
        print(f"\n❌ FAILED at proposal step: [{err.get('code')}] {err.get('message')}")
        print("\n   This is the exact reason Deriv is refusing to even quote a price.")
        print("   Common causes: market closed for this symbol right now, account")
        print("   type can't trade (Wallet account), or currency mismatch.")
        await ws.close()
        sys.exit(1)

    proposal = proposal_resp["proposal"]
    print(f"✅ Got a proposal.")
    print(f"   Ask price: {proposal.get('ask_price')}")
    print(f"   Potential payout: {proposal.get('payout')}")
    print(f"   Current spot: {proposal.get('spot')}")

    print(f"\nAttempting to BUY this contract for real (${TEST_STAKE})...")
    buy_resp = await send(ws, {"buy": proposal["id"], "price": proposal["ask_price"]})

    if buy_resp.get("error"):
        err = buy_resp["error"]
        print(f"\n❌ FAILED at buy step: [{err.get('code')}] {err.get('message')}")
        print("\n   This is the DEFINITIVE reason trades aren't executing.")
        print("   Whatever this message says is the real, root problem —")
        print("   share this exact error and we fix that specific thing.")
        await ws.close()
        sys.exit(1)

    buy = buy_resp["buy"]
    print(f"\n✅✅✅ SUCCESS — a real contract was purchased!")
    print(f"   Contract ID: {buy.get('contract_id')}")
    print(f"   Buy price: {buy.get('buy_price')}")
    print(f"   Payout if it wins: {buy.get('payout')}")
    print(f"\n   THIS PROVES: your Deriv account, token, and trade execution")
    print(f"   all work correctly. If the full bot still shows 'no qualifying")
    print(f"   signals', the problem is 100% in the AI/consensus layer, not Deriv.")

    await ws.close()


if __name__ == "__main__":
    asyncio.run(main())
