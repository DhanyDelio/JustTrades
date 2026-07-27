import math

class PortfolioManager:
    def __init__(self, repo, budget_usd, lab_starting_capital, per_trade_budget):
        self.repo = repo
        self.budget_usd = budget_usd
        self.lab_starting_capital = lab_starting_capital
        self.per_trade_budget = per_trade_budget

    def get_simulated_balance(self, trades: list[dict] | None = None) -> float:
        """
        Compute simulated capital = self.budget_usd + sum of all realized PnL
        from closed trades (TP_HIT or SL_HIT).
    
        This is the only number used for:
          - Single vs dual position threshold comparison
          - Per-slot budget allocation
          - Position sizing
    
        It intentionally ignores testnet wallet balance (which is fake/large)
        and only grows/shrinks based on real trade outcomes logged here.
        """
        if trades is None:
            trades = self.repo.load_trade_log()
        closed_pnl = sum(
            t.get("realized_pnl_usd") or 0.0
            for t in trades
            if t.get("exit_status") in ("TP_HIT", "SL_HIT")
            and t.get("realized_pnl_usd") is not None
        )
        return self.budget_usd + closed_pnl


    def compute_lab_pool(self, trades: list[dict] | None = None) -> dict:
        """
        Compute the compounding lab capital pool used by --propose-all batches.
    
        Returns a dict with keys:
          - lab_capital: self.lab_starting_capital + sum(realized_pnl_usd for resolved clustered trades)
          - deployed_capital: sum(self.per_trade_budget) for clustered OPEN trades
          - available_capital: lab_capital - deployed_capital
          - max_new_positions: floor(available_capital / self.per_trade_budget)
    
        Note: only trades with a non-null `correlation_cluster_id` are considered part of the
        lab/batch pool. This keeps it separate from single `--propose` simulated balance.
        """
        import math
        if trades is None:
            trades = self.repo.load_trade_log()
    
        # Realized PnL only from resolved clustered trades
        closed_cluster_pnl = sum(
            (t.get("realized_pnl_usd") or 0.0)
            for t in trades
            if t.get("correlation_cluster_id") and t.get("exit_status") in ("TP_HIT", "SL_HIT")
        )
    
        lab_capital = self.lab_starting_capital + closed_cluster_pnl
    
        # Deployed capital: open clustered trades
        deployed_count = sum(
            1 for t in trades
            if t.get("correlation_cluster_id") and t.get("exit_status") == "OPEN"
        )
        deployed_capital = deployed_count * self.per_trade_budget
    
        available_capital = lab_capital - deployed_capital
        max_new_positions = math.floor(max(0.0, available_capital) / self.per_trade_budget)
    
        return {
            "lab_capital": lab_capital,
            "closed_cluster_pnl": closed_cluster_pnl,
            "deployed_capital": deployed_capital,
            "available_capital": available_capital,
            "max_new_positions": int(max_new_positions),
            "deployed_count": deployed_count,
        }


