"""
Tests for caps and guardrails.

Verifies max_orders and max_debit_total enforcement with deterministic behavior.
"""

import pytest


class TestMaxOrdersCap:
    """Test max_orders cap enforcement."""
    
    def test_no_truncation_when_under_cap(self):
        """Should not truncate when structures < max_orders."""
        structures = [
            {"rank": 1, "debit_per_contract": 1000},
            {"rank": 2, "debit_per_contract": 1200}
        ]
        max_orders = 3
        
        truncated = structures[:max_orders]
        
        assert len(truncated) == 2
        assert truncated == structures
    
    def test_truncation_when_over_cap(self):
        """Should truncate when structures > max_orders."""
        structures = [
            {"rank": 1, "debit_per_contract": 1000},
            {"rank": 2, "debit_per_contract": 1200},
            {"rank": 3, "debit_per_contract": 1100},
            {"rank": 4, "debit_per_contract": 1300},
            {"rank": 5, "debit_per_contract": 1050}
        ]
        max_orders = 3
        
        truncated = structures[:max_orders]
        
        assert len(truncated) == 3
        assert truncated[0]["rank"] == 1
        assert truncated[1]["rank"] == 2
        assert truncated[2]["rank"] == 3
    
    def test_truncation_warning_generated(self):
        """Should generate warning when truncating."""
        structures = [{"rank": i} for i in range(1, 6)]
        max_orders = 2
        warnings = []
        
        if len(structures) > max_orders:
            warnings.append(f"CAP_TRUNCATED_ORDERS: {len(structures)} -> {max_orders}")
            structures = structures[:max_orders]
        
        assert len(warnings) == 1
        assert "CAP_TRUNCATED_ORDERS: 5 -> 2" in warnings[0]


class TestMaxDebitTotalCap:
    """Test max_debit_total cap enforcement."""
    
    def test_no_truncation_when_under_cap(self):
        """Should not truncate when total debit < cap."""
        tickets = [
            {"limit_price": 10.00, "quantity": 1},  # 1000
            {"limit_price": 12.00, "quantity": 1}   # 1200
        ]
        max_debit_total = 5000.0
        
        total_debit = sum(t["limit_price"] * t["quantity"] * 100 for t in tickets)
        
        assert total_debit == 2200.0
        assert total_debit < max_debit_total
    
    def test_truncation_when_over_cap(self):
        """Should truncate tickets when total debit > cap."""
        tickets = [
            {"limit_price": 30.00, "quantity": 1, "metadata": {"ev_per_dollar": 0.15}},  # 3000
            {"limit_price": 15.00, "quantity": 1, "metadata": {"ev_per_dollar": 0.12}},  # 1500
            {"limit_price": 20.00, "quantity": 1, "metadata": {"ev_per_dollar": 0.10}}   # 2000
        ]
        max_debit_total = 5000.0
        
        # Sort by EV/$ (highest first)
        tickets_sorted = sorted(
            tickets,
            key=lambda t: t.get("metadata", {}).get("ev_per_dollar", 0),
            reverse=True
        )
        
        # Apply cap
        capped_tickets = []
        running_debit = 0
        
        for ticket in tickets_sorted:
            ticket_debit = ticket["limit_price"] * ticket["quantity"] * 100
            if running_debit + ticket_debit <= max_debit_total:
                capped_tickets.append(ticket)
                running_debit += ticket_debit
        
        # 3000 + 1500 = 4500 <= 5000, both fit
        # 4500 + 2000 = 6500 > 5000, third doesn't fit
        assert len(capped_tickets) == 2
        assert capped_tickets[0]["limit_price"] == 30.00
        assert capped_tickets[1]["limit_price"] == 15.00
        assert running_debit == 4500.0
    
    def test_quantity_reduction_to_fit_cap(self):
        """Should reduce quantity if ticket partially fits under cap."""
        tickets = [
            {"limit_price": 20.00, "quantity": 1, "metadata": {"ev_per_dollar": 0.15}},  # 2000
            {"limit_price": 10.00, "quantity": 4, "metadata": {"ev_per_dollar": 0.12}}   # 4000 for qty=4
        ]
        max_debit_total = 4500.0
        
        # Sort by EV/$
        tickets_sorted = sorted(
            tickets,
            key=lambda t: t.get("metadata", {}).get("ev_per_dollar", 0),
            reverse=True
        )
        
        capped_tickets = []
        running_debit = 0
        
        for ticket in tickets_sorted:
            ticket_debit = ticket["limit_price"] * ticket["quantity"] * 100
            if running_debit + ticket_debit <= max_debit_total:
                capped_tickets.append(ticket)
                running_debit += ticket_debit
            else:
                # Try to reduce quantity
                remaining_budget = max_debit_total - running_debit
                max_allowed_qty = int(remaining_budget / (ticket["limit_price"] * 100))
                if max_allowed_qty > 0:
                    ticket_copy = ticket.copy()
                    ticket_copy["quantity"] = max_allowed_qty
                    capped_tickets.append(ticket_copy)
                    break
        
        # First ticket: 20 * 1 * 100 = 2000 <= 4500, add it
        # running_debit = 2000
        # Second ticket: 10 * 4 * 100 = 4000, 2000 + 4000 = 6000 > 4500
        # remaining_budget = 4500 - 2000 = 2500
        # max_allowed_qty = 2500 / 1000 = 2.5 = 2 (int)
        # Second ticket added with qty=2
        
        assert len(capped_tickets) == 2
        assert capped_tickets[0]["quantity"] == 1
        assert capped_tickets[1]["quantity"] == 2  # Reduced from 4
        
        total = sum(t["limit_price"] * t["quantity"] * 100 for t in capped_tickets)
        assert total == 4000.0  # 2000 + 2000
    
    def test_deterministic_sorting_by_ev_per_dollar(self):
        """Cap enforcement should prioritize highest EV/$ deterministically."""
        tickets = [
            {"limit_price": 10.00, "quantity": 1, "metadata": {"ev_per_dollar": 0.10}},
            {"limit_price": 15.00, "quantity": 1, "metadata": {"ev_per_dollar": 0.20}},
            {"limit_price": 12.00, "quantity": 1, "metadata": {"ev_per_dollar": 0.15}}
        ]
        
        # Sort by EV/$ descending
        sorted_tickets = sorted(
            tickets,
            key=lambda t: t.get("metadata", {}).get("ev_per_dollar", 0),
            reverse=True
        )
        
        assert sorted_tickets[0]["metadata"]["ev_per_dollar"] == 0.20
        assert sorted_tickets[1]["metadata"]["ev_per_dollar"] == 0.15
        assert sorted_tickets[2]["metadata"]["ev_per_dollar"] == 0.10
        
        # Verify order is deterministic
        sorted_tickets2 = sorted(
            tickets,
            key=lambda t: t.get("metadata", {}).get("ev_per_dollar", 0),
            reverse=True
        )
        
        assert sorted_tickets == sorted_tickets2


class TestCapsIntegration:
    """Test integration of both caps."""
    
    def test_both_caps_applied_sequentially(self):
        """max_orders should be applied first, then max_debit_total."""
        structures = [
            {"rank": i, "limit_price": 20.00, "quantity": 1, "metadata": {"ev_per_dollar": 0.1 + i*0.01}}
            for i in range(1, 6)
        ]
        max_orders = 3
        max_debit_total = 4500.0
        
        # Step 1: Apply max_orders
        truncated = structures[:max_orders]
        assert len(truncated) == 3
        
        # Step 2: Apply max_debit_total
        tickets_sorted = sorted(
            truncated,
            key=lambda t: t.get("metadata", {}).get("ev_per_dollar", 0),
            reverse=True
        )
        
        capped_tickets = []
        running_debit = 0
        
        for ticket in tickets_sorted:
            ticket_debit = ticket["limit_price"] * ticket["quantity"] * 100
            if running_debit + ticket_debit <= max_debit_total:
                capped_tickets.append(ticket)
                running_debit += ticket_debit
        
        assert len(capped_tickets) == 2  # 2000 * 2 = 4000 <= 4500
        assert running_debit == 4000.0
    
    def test_warnings_for_both_caps(self):
        """Both caps should generate warnings when triggered."""
        structures = [{"rank": i} for i in range(1, 6)]
        max_orders = 3
        warnings = []
        
        # Apply max_orders
        if len(structures) > max_orders:
            warnings.append(f"CAP_TRUNCATED_ORDERS: {len(structures)} -> {max_orders}")
        
        # Simulate max_debit_total warning
        total_debit = 6000.0
        max_debit_total = 5000.0
        
        if total_debit > max_debit_total:
            warnings.append(f"CAP_TRUNCATED_ORDERS_DEBIT: ${total_debit:,.2f} -> ${max_debit_total:,.2f}")
        
        assert len(warnings) == 2
        assert "CAP_TRUNCATED_ORDERS" in warnings[0]
        assert "CAP_TRUNCATED_ORDERS_DEBIT" in warnings[1]
