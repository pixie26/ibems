"""One-off live account inspector (NOT for production). Prints real account
summary, balances, and positions from the IB Gateway."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

from ib_async import IB, Stock


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4002)
    p.add_argument("--client-id", type=int, default=990)
    p.add_argument("--symbol", default="SPY")
    args = p.parse_args()

    ib = IB()
    errors: list[str] = []
    ib.errorEvent += lambda req, code, msg, contract: errors.append(f"[{code}] {msg}")

    print(f"Connecting to {args.host}:{args.port} (clientId={args.client_id}) ...")
    ib.connect(args.host, args.port, clientId=args.client_id, timeout=10, readonly=True)
    print(f"  connected={ib.isConnected()}  server={ib.client.serverVersion()}")

    # ---- server time / clock skew ----
    server_time = ib.reqCurrentTime().astimezone(timezone.utc)
    skew = (datetime.now(timezone.utc) - server_time).total_seconds()
    print(f"\n[Server time] {server_time.isoformat()}  (clock skew {skew:+.1f}s)")

    # ---- managed accounts ----
    accts = ib.managedAccounts()
    print(f"\n[Managed accounts] {accts}  (count={len(accts)})")

    # ---- account summary (balance / net liquidation) ----
    # ib_async 2.1.0: reqAccountSummary() returns None (state-based). Use the
    # non-blocking accountSummary() cache, which auto-subscribes on connect.
    print("\n[Account summary]")
    interest = {
        "NetLiquidation", "TotalCashValue", "AvailableFunds", "EquityWithLoanValue",
        "GrossPositionValue", "InitMarginReq", "MaintMarginReq", "BuyingPower",
        "CashBalance", "AccruedCash", "RealizedPnL", "UnrealizedPnL",
    }
    try:
        ib.reqAccountSummary()  # ensure subscribed
        ib.sleep(1.0)
        rows = list(ib.accountSummary())
        if not rows:
            print("  (empty — gateway returned no account-summary rows)")
        shown = sorted([r for r in rows if r.tag in interest],
                       key=lambda r: (r.account, r.tag))
        for row in shown:
            print(f"  {row.account}  {row.tag:24s} = {row.value:>18s}  {row.currency}")
        all_tags = sorted({r.tag for r in rows})
        print(f"  ({len(rows)} rows total, {len(all_tags)} distinct tags)")
    except Exception as e:
        print(f"  ERROR accountSummary: {e!r}")

    # ---- positions ----
    print("\n[Positions]")
    positions = ib.reqPositions()
    if not positions:
        print("  (no open positions)")
    for pos in sorted(positions, key=lambda p: (p.account, p.contract.conId)):
        c = pos.contract
        print(
            f"  {pos.account}  conId={c.conId}  {c.symbol} {c.secType} "
            f"position={pos.position}  avgCost={pos.avgCost:.4f}"
        )
    print(f"  total positions = {len(positions)}")

    # ---- open orders ----
    print("\n[Open orders]")
    trades = ib.reqAllOpenOrders()
    if not trades:
        print("  (no open orders)")
    for t in trades:
        o = t.order
        print(
            f"  {o.account}  {t.contract.symbol}  {o.action} {o.totalQuantity} "
            f"{o.orderType}  status={t.orderStatus.status}"
        )
    print(f"  total open orders = {len(trades)}")

    # ---- market data probe ----
    print(f"\n[Market data probe] {args.symbol}")
    contract = Stock(args.symbol, "SMART", "USD", primaryExchange="ARCA")
    ib.qualifyContracts(contract)
    ticker = ib.reqMktData(contract, "", False, False)
    ib.sleep(2.0)
    print(
        f"  marketDataType={ticker.marketDataType} "
        f"(1=Live, 3=Delayed, 4=DelayedFrozen)"
    )
    print(
        f"  last={ticker.last}  bid={ticker.bid}  ask={ticker.ask}  "
        f"close={ticker.close}  time={ticker.time}"
    )
    ib.cancelMktData(contract)

    if errors:
        print("\n[Errors observed]")
        for e in errors:
            print(f"  {e}")

    ib.disconnect()
    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
