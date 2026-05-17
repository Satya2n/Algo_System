# Documentation: `engine/risk.py`

## Overview
This module translates abstract risk parameters (e.g., "Risk 2% of Capital") into concrete, actionable trade quantities.

## Core Responsibilities
1. **Max Loss Calculation (`estimate_max_loss_per_lot`)**
   Calculates the absolute worst-case scenario for the credit spread: 
   `Max Loss = (Spread Width - Net Credit) * Lot Size`.
2. **Position Sizing (`position_size_for_spread`)**
   Determines how many lots the strategy *wants* to trade based on the 2% risk limit.
3. **Broker Margin Capping (`get_required_broker_margin`)**
   Takes the desired position size and multiplies it by `ESTIMATED_MARGIN_PER_LOT` (₹45,000).

## Edge Cases Handled
- **The "Over-Sizing" Edge Case:** If the spread width is extremely narrow (e.g., ₹20), the `Max Loss` is tiny. A naive risk model might tell the algorithm to buy 50 lots to reach the 2% capital risk limit. However, 50 lots require `50 * 45,000 = ₹22,50,000` in broker margin. The `ExecutionEngine` queries this `get_required_broker_margin` function and caps the 50 lots down to the actual number of lots your Dhan account can support.
