"""Winner-vs-loser feature separation from scan logs (declared diagnostics).

Companion to backtest-scan: reads losses-*.jsonl / wins-*.jsonl pairs and
scores each logged feature by sign agreement and scaled mean difference.
Output is hypothesis-forming: separation here is in-sample and would need a
held-out window before any rule is frozen.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import timezone
from pathlib import Path
from statistics import mean, pstdev

SIGN_FEATURES = ("setup_delta", "window_cvd", "delta", "setup_poc", "bar_poc")
NUMERIC_FEATURES = ("cluster_bars", "zone_center", "profile_poc", "entry", "stop", "target",
                    "net_reward_risk", "expected_cost_fraction", "profile_trade_count",
                    "setup_trade_count", "trade_count", "buy_volume", "sell_volume")


def rows(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def separation(winners, losers, features):
    report = {}
    for name in features:
        w = [row["features"].get(name) for row in winners if row["features"].get(name) is not None]
        l = [row["features"].get(name) for row in losers if row["features"].get(name) is not None]
        if not w or not l:
            continue
        pooled = pstdev(w + l) or 1e-12
        report[name] = {"win_mean": round(mean(w), 4), "loss_mean": round(mean(l), 4),
                        "scaled_gap": round(abs(mean(w) - mean(l)) / pooled, 3)}
    return dict(sorted(report.items(), key=lambda item: -item[1]["scaled_gap"]))


def sign_agreement(winners, losers, feature):
    def stats(group):
        values = [row["features"].get(feature) for row in group if row["features"].get(feature) is not None]
        return {"n": len(values), "positive_frac": round(sum(1 for v in values if v > 0) / len(values), 3)} if values else {"n": 0}
    return {"winners": stats(winners), "losers": stats(losers)}


def analyze(prefix):
    result = {"prefix": prefix}
    for direction in ("long", "short"):
        loss_lines = rows(f"{prefix}-losses-{direction}.jsonl")
        win_lines = rows(f"{prefix}-wins-{direction}.jsonl")
        outcome = {"wins": len(win_lines), "losses": len(loss_lines)}
        outcome["scale_gaps"] = separation(win_lines, loss_lines, SIGN_FEATURES + NUMERIC_FEATURES)
        outcome["exit_reasons"] = {"wins": {reason: sum(1 for r in win_lines if r["exit_reason"] == reason)
                                            for reason in ("stop", "target", "max_holding", "end_of_data", "daily_loss_limit")},
                                   "losses": {reason: sum(1 for r in loss_lines if r["exit_reason"] == reason)
                                              for reason in ("stop", "target", "max_holding", "end_of_data", "daily_loss_limit")}}
        outcome["fee_per_trade"] = {"wins": round(mean(r["entry_fee"] + r["exit_fee"] for r in win_lines), 4) if win_lines else None,
                                    "losses": round(mean(r["entry_fee"] + r["exit_fee"] for r in loss_lines), 4) if loss_lines else None}
        outcome["mean_net_r"] = {"wins": round(mean(r["net_r"] for r in win_lines), 3) if win_lines else None,
                                 "losses": round(mean(r["net_r"] for r in loss_lines), 3) if loss_lines else None}
        for feature in ("setup_delta", "window_cvd"):
            outcome[f"sign_{feature}"] = sign_agreement(win_lines, loss_lines, feature)
        result[direction] = outcome
    return result


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("prefix", help="Loss/win file prefix, e.g. data/exp-limit2k")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()
    result = analyze(args.prefix)
    text = json.dumps(result, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n")
    print(text)


if __name__ == "__main__":
    main()