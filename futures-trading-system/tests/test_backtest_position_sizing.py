"""Position accounting and sizing."""

import pytest

from src.backtest.config import CostConfig, SizingConfig, SlippageConfig
from src.backtest.position import Position
from src.backtest.sizing import position_size
from tests.backtest_helpers import MES, MNQ


def test_contract_specs_and_tick_values():
    assert (MES.tick_size, MES.point_value, MES.tick_value) == (0.25, 5.0, 1.25)
    assert (MNQ.tick_size, MNQ.point_value, MNQ.tick_value) == (0.25, 2.0, 0.5)


def test_configured_specs_match_cme(config):
    assert config.contracts["MES"].tick_value == 1.25
    assert config.contracts["MNQ"].tick_value == 0.5


def test_average_price_and_realized_pnl():
    p = Position(point_value=5.0)
    assert p.apply_fill(True, 1, 100.0) == 0.0
    assert p.apply_fill(True, 1, 102.0) == 0.0
    assert (p.quantity, p.avg_price) == (2, 101.0)
    assert p.unrealized_pnl(103.0) == (103 - 101) * 2 * 5
    assert p.apply_fill(False, 1, 105.0) == pytest.approx((105 - 101) * 5)  # 20
    assert (p.quantity, p.avg_price) == (1, 101.0)
    assert p.apply_fill(False, 1, 99.0) == pytest.approx(-10.0)
    assert p.is_flat and p.avg_price == 0.0


def test_short_and_reversal():
    p = Position(point_value=2.0)
    p.apply_fill(False, 2, 200.0)
    assert p.unrealized_pnl(190.0) == 40.0
    assert p.apply_fill(True, 3, 190.0) == pytest.approx(40.0)  # cover 2, open 1 long
    assert (p.quantity, p.avg_price) == (1, 190.0)
    with pytest.raises(ValueError):
        p.apply_fill(True, 0, 1.0)


def test_fixed_and_requested_sizes():
    assert position_size(SizingConfig("fixed", 2), MES, entry_price=5000, stop_loss=None).quantity == 2
    assert position_size(SizingConfig("fixed", 2), MES, entry_price=5000, stop_loss=None, requested=1).quantity == 1
    assert position_size(SizingConfig(), MES, entry_price=5000, stop_loss=None, requested=0).reason == "invalid_quantity"


def test_risk_based_sizing_hand_calculated():
    cfg = SizingConfig("risk", risk_per_trade=200.0, include_costs_in_risk=True)
    cost, slip = CostConfig(1.0), SlippageConfig(1, 1, 0)
    # MES: 10 pts * $5 = 50 + 2 * $1 commission + (1+1) ticks * $1.25 = 54.5 -> floor(200/54.5) = 3
    assert position_size(cfg, MES, entry_price=5000, stop_loss=4990, cost=cost, slippage=slip).quantity == 3
    # MNQ: 10 pts * $2 = 20 + 2 + 2 * $0.50 = 23 -> floor(200/23) = 8
    assert position_size(cfg, MNQ, entry_price=20000, stop_loss=19990, cost=cost, slippage=slip).quantity == 8
    no_costs = SizingConfig("risk", risk_per_trade=200.0, include_costs_in_risk=False)
    assert position_size(no_costs, MES, entry_price=5000, stop_loss=4990).quantity == 4
    assert position_size(no_costs, MES, entry_price=5000, stop_loss=None).reason == "risk_sizing_requires_stop_loss"
    tiny = SizingConfig("risk", risk_per_trade=10.0)
    assert position_size(tiny, MES, entry_price=5000, stop_loss=4990).reason == "size_below_one_contract"
