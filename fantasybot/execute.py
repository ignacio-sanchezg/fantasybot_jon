"""EXECUTION layer: turns decisions into real actions.

Autonomy authorized by the user:
  - Lineup: applies the best lineup (reversible, no spending).
  - Bid/cancel in market: places bids on profitable flips and pulls those that no
    longer apply (reversible until market close). May use the whole balance.
  - Buyouts: NOT automatic (irreversible spending) → left as an alert/task.

Everything runs through `dry_run`: if True, it only returns the PLAN without
touching anything.
"""

from . import events, state
from .strategy import flip
from .strategy.lineup import payload_ids


def apply_lineup(client, team_id, best, current_ids, dry_run=True):
    """Applies the optimal lineup if it differs from the current one."""
    new_ids = payload_ids(best)
    if new_ids == current_ids:
        return {"action": "lineup", "changed": False}
    if not dry_run:
        client.update_lineup(team_id, best["payload"])
        d, m, f = best["formation"]
        events.emit("lineup", f"Lineup {d}-{m}-{f} applied",
                    detail={"score": best.get("total")})
    return {"action": "lineup", "changed": True, "applied": not dry_run,
            "formation": best["formation"]}


def _system_flips(client, league_id):
    """Profitable SYSTEM flips (a single pass over market + trends)."""
    return [o for o in flip.opportunities(client, league_id)
            if o["via"] == "SISTEMA" and o["margin_pct"] > 0]


def plan_bids(client, league_id, team, ops=None):
    """What to bid on: profitable SYSTEM flips that fit the balance, by margin.

    SYSTEM only (auction). Buyouts are outside the scope of autonomy.
    """
    money = team["teamMoney"]
    if ops is None:
        ops = _system_flips(client, league_id)
    # The live market is the truth about what already has money on it, not the local
    # file. The file only remembers THIS bot's bids: anything placed from the app or
    # by hand is invisible to it, and the bot re-proposes players that already carry
    # a bid. Reading `bid`/`offer` off each listing catches them all.
    already = set(state.load_bids())
    try:
        for el in client.market(league_id):
            if el.get("bid") or el.get("offer"):
                already.add(str(el.get("id")))
    except Exception:
        pass          # without the market, the local file is still better than nothing
    plan, committed = [], 0
    for o in ops:
        if str(o["market_id"]) in already:
            continue  # money is already on that player
        if committed + o["buy_price"] > money:
            continue  # doesn't fit in the balance
        plan.append({"market_id": o["market_id"], "nombre": o["nombre"],
                     "amount": o["buy_price"], "margin_pct": o["margin_pct"]})
        committed += o["buy_price"]
    return plan


def sync_bids(client, league_id, team, dry_run=True):
    """Places new bids from the plan and cancels those that no longer apply."""
    ops = _system_flips(client, league_id)   # a single pass, reused below
    plan = plan_bids(client, league_id, team, ops)
    bids = state.load_bids()
    valid_ids = {o["market_id"] for o in ops}

    placed, cancelled = [], []
    # cancel bids whose target is no longer profitable
    for mid, info in list(bids.items()):
        if mid not in valid_ids:
            if not dry_run:
                try:
                    client.cancel_bid(league_id, mid, info["bid_id"])
                except Exception:
                    pass
                bids.pop(mid, None)
                events.emit("cancel", f"Bid cancelled: {info.get('nombre', mid)}",
                            detail="no longer profitable")
            cancelled.append(info.get("nombre", mid))
    # place new bids
    for b in plan:
        if not dry_run:
            resp = client.make_bid(league_id, b["market_id"], b["amount"])
            bid_id = resp.get("id") if isinstance(resp, dict) else None
            bids[b["market_id"]] = {"bid_id": bid_id, "amount": b["amount"],
                                    "nombre": b["nombre"]}
            events.emit("bid", f"Bid {b['amount']:,} for {b['nombre']}",
                        detail={"margin": f"{b['margin_pct']}%"})
        placed.append(b)

    if not dry_run:
        state.save_bids(bids)
    return {"action": "bids", "placed": placed, "cancelled": cancelled,
            "applied": not dry_run}


def act(client, league_id, team_id, team, best, current_ids, dry_run=True):
    """Executes (or plans) the autonomous actions: set lineup + bid."""
    return {
        "lineup": apply_lineup(client, team_id, best, current_ids, dry_run),
        "bids": sync_bids(client, league_id, team, dry_run),
    }
