"""Shared-cash portfolio engine, independent of Flask and single-stock accounts.

All mutations are prepared on a private copy, checked, committed in one SQLite
transaction, then published in memory. Only advance can move the owned replay.
"""

from copy import deepcopy
from datetime import timedelta
from threading import RLock
from uuid import uuid4

from backend.portfolio_ledger_store import PortfolioLedgerStore
from backend.portfolio_models import (
    Account, DailyAsset, Fill, LedgerEntry, Order, PortfolioState, PositionLot, serialize, restore_state,
)
from backend.portfolio_rules import FeeSchedule, MarketRules, ZERO, money, quantity
from backend.replay_models import ReplayDataError, day, number, stock_code, utc_now


def identifier(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ReplayDataError("操作标识必须是 1 至 128 个字符")
    return value


class PortfolioEngine:
    def __init__(self, replay, initial_cash="100000", *, fees=None, rules=None, store=None,
                 training_id=None):
        initial = number(initial_cash)
        if initial <= 0 or initial != money(initial):
            raise ReplayDataError("初始资金必须大于零且最多保留两位小数")
        self.__replay = replay._engine_fork()
        self.__fees = deepcopy(fees) if fees is not None else FeeSchedule()
        self.__rules = rules or MarketRules()
        self.__owns_store = store is None
        self.training_id = identifier(training_id or str(uuid4()))
        self.__lock = RLock()
        calendar, _, actions = self.__replay._engine_context()
        # Gate every execution date up front; no silent use of current rates.
        dates = calendar.between(self.__replay.current_date, self.__replay._engine_end_date())
        for code, history in actions.items():
            history.require_supported(self.__replay.current_date,
                                      self.__replay._engine_end_date(),
                                      synthetic=self.__replay.snapshot()["synthetic"], accounting=True)
            for event in history.events:
                if self.__replay.current_date <= event.effective_on <= self.__replay._engine_end_date():
                    if not calendar.is_open(event.record_on) or not calendar.is_open(event.effective_on):
                        raise ReplayDataError("公司行为日期不在公共交易日历中")
        for value in dates:
            self.__fees.for_date(value)
        state = PortfolioState(self.__replay.current_date, Account(money(initial), money(initial)))
        self._entry(state, "opening", initial, reference="initial_cash")
        self._record_entitlements(state, self.__replay)
        self._mark(state, self.__replay)
        self._check(state)
        self.__store = store or PortfolioLedgerStore()
        try:
            self.__store.save(self.training_id, state, None)
        except BaseException:
            if self.__owns_store:
                self.__store.close()
            raise
        self.__state = state

    def close(self):
        if self.__owns_store:
            self.__store.close()

    @classmethod
    def restore(cls, replay, store, training_id, *, fees=None, rules=None):
        """Rehydrate without inserting an opening entry or replaying any fills."""
        payload = store.read(training_id)
        if payload is None:
            raise ReplayDataError("组合训练不存在")
        result = cls.__new__(cls)
        result.training_id = identifier(training_id)
        result.__store, result.__owns_store = store, False
        result.__fees = deepcopy(fees) if fees is not None else FeeSchedule()
        result.__rules = rules or MarketRules()
        result.__lock = RLock()
        result.__state = restore_state(payload)
        result.__replay = replay._engine_fork()
        result.__replay._engine_seek(result.__state.current_date)
        result._check(result.__state)
        if not result.__state.daily_assets or result.__state.daily_assets[-1].date != result.current_date:
            raise ReplayDataError("资产记录与组合日期不一致")
        return result

    def _require_active(self):
        if self.__state.status != "active":
            raise ReplayDataError("组合训练已结束，不能继续交易或推进")

    def end(self):
        with self.__lock:
            if self.__state.status == "ended":
                return self.snapshot()
            state = deepcopy(self.__state)
            for order in state.orders:
                if order.status == "pending":
                    self._finish_order(order, "cancelled", "组合训练结束，释放冻结资金及股数")
                    self._entry(state, "order_cancelled", reference=order.order_id)
            state.status, state.ended_at = "ended", utc_now()
            self._entry(state, "training_ended")
            return self._publish(state)

    def report(self):
        with self.__lock:
            if self.__state.status != "ended":
                raise ReplayDataError("请先结束组合训练再查看完整报告")
            snapshot = self.snapshot()
            last = self.__state.daily_assets[-1]
            peak, drawdown = self.__state.account.initial_cash, ZERO
            for asset in self.__state.daily_assets:
                peak = max(peak, asset.total_assets)
                drawdown = max(drawdown, (peak - asset.total_assets) / peak)
            full_replay = self.__replay._engine_fork()
            full_replay._engine_seek(full_replay._engine_end_date(), validate=False)
            return {"training_id": self.training_id, "status": "ended", "account": snapshot,
                    "summary": serialize({"initial_cash": self.__state.account.initial_cash,
                        "final_assets": last.total_assets,
                        "total_pnl": last.total_assets - self.__state.account.initial_cash,
                        "return_percent": money((last.total_assets / self.__state.account.initial_cash - 1) * 100),
                        "max_drawdown_percent": money(drawdown * 100),
                        "fill_count": len(self.__state.fills), "valuation_date": self.current_date,
                        "total_fees": sum((fill.fees["total"] for fill in self.__state.fills), ZERO),
                        "realized_pnl": self.__state.account.realized_pnl,
                        "unrealized_pnl": last.unrealized_pnl}),
                    "history": {code: full_replay.history(code) for code in full_replay.stocks},
                    "note": "按结束日期收盘估值，未强制平仓；完整行情仅用于结束后复盘。日线简化模拟。"}

    @property
    def current_date(self):
        return self.__state.current_date

    @staticmethod
    def _entry(state, kind, cash=ZERO, *, stock=None, qty=0, cost=ZERO,
               realized=ZERO, reference="", details=None):
        state.ledger.append(LedgerEntry(str(uuid4()), state.current_date, kind, money(cash),
                                       stock, qty, money(cost), money(realized), reference,
                                       deepcopy(details or {})))

    @staticmethod
    def _frozen_cash(state):
        return sum((order.frozen_cash for order in state.orders if order.status == "pending"), ZERO)

    @staticmethod
    def _frozen_shares(state, code):
        return sum(order.frozen_shares for order in state.orders
                   if order.stock == code and order.status == "pending")

    @staticmethod
    def _eligible(lot, value):
        return lot.acquired_on < value and lot.available_on <= value

    def _available_shares(self, state, code, execute_on):
        return sum(lot.quantity for lot in state.lots if lot.stock == code and self._eligible(lot, execute_on)) - self._frozen_shares(state, code)

    def _publish(self, state, replay=None, *, operation=None):
        self._check(state)
        state.version = self.__state.version + 1
        result = self._snapshot(state, replay or self.__replay)
        if operation is not None:
            result["operation_id"] = operation[0]
        saved_operation = None if operation is None else (*operation, result)
        self.__store.save(self.training_id, state, self.__state.version, operation=saved_operation)
        self.__state = state
        if replay is not None:
            self.__replay = replay
        return result

    def submit(self, stock, side, qty, *, request_id, budget=None):
        """Freeze a per-order maximum spend; retries must reuse request_id.

        Today's fills can be reserved for TOMORROW's open. sellable_now still
        excludes them. Actual matching also checks lot date and share credit.
        """
        with self.__lock:
            self._require_active()
            stock, request_id = stock_code(stock), identifier(request_id)
            quantity(qty)
            if side not in {"buy", "sell"}:
                raise ReplayDataError("买卖方向必须为 buy 或 sell")
            if stock not in self.__replay.stocks:
                raise ReplayDataError("股票不在本次训练股票池中")
            normalized_budget = None
            if budget is not None:
                value = number(budget)
                if side != "buy" or value <= 0 or value != money(value):
                    raise ReplayDataError("买入预算必须是正数且最多两位小数")
                normalized_budget = money(value)
            fingerprint = serialize([stock, side, qty, normalized_budget])
            old = self.__state.requests.get(request_id)
            if old:
                if old["fingerprint"] != fingerprint:
                    raise ReplayDataError("重复订单标识对应了不同参数")
                return serialize(next(o for o in self.__state.orders if o.order_id == old["order_id"]))
            target = self.__replay._engine_next_date()
            state = deepcopy(self.__state)
            order = Order(str(uuid4()), request_id, stock, side, qty, state.current_date,
                          target, len(state.orders) + 1, rule_version=self.__rules.version)
            state.orders.append(order)
            state.requests[request_id] = {"fingerprint": fingerprint, "order_id": order.order_id}
            try:
                rule = self.__fees.for_date(target)
                order.fee_version = rule.version
                self.__rules.validate_quantity(side, qty, self._available_shares(state, stock, target))
                quote = self.__replay._engine_state(stock)
                if not quote.execution_data_ready:
                    raise ReplayDataError(quote.reason or "当前无可用行情，不能提交订单")
                if side == "buy":
                    order.estimated_price = money(quote.bar.close * (1 + self.__rules.reserve_buffer))
                    gross = money(order.estimated_price * qty)
                    order.estimated_fees = self.__fees.calculate(target, side, gross)
                    required = gross + order.estimated_fees["total"]
                    order.frozen_cash = normalized_budget if normalized_budget is not None else required
                    if order.frozen_cash < money(quote.bar.close * qty) + self.__fees.calculate(target, side, quote.bar.close * qty)["total"]:
                        raise ReplayDataError("预算不足以覆盖当前收盘估算金额及费用")
                    if self._frozen_cash(state) > state.account.cash:
                        raise ReplayDataError("可用资金不足，其他订单已占用资金")
                else:
                    order.estimated_price = money(quote.bar.close)
                    order.estimated_fees = self.__fees.calculate(target, side, order.estimated_price * qty)
                    order.frozen_shares = qty
            except ReplayDataError as exc:
                self._finish_order(order, "rejected", str(exc))
            self._entry(state, "order_submitted", reference=order.order_id,
                        details={"status": order.status, "reason": order.reason})
            self._publish(state)
            return serialize(order)

    def estimate(self, stock, side, qty):
        """Apply submission rules to a disposable engine, never the live store."""
        with self.__lock:
            self._require_active()
            preview = self.__class__.__new__(self.__class__)
            preview.training_id = self.training_id
            preview.__state = deepcopy(self.__state)
            preview.__replay = self.__replay._engine_fork()
            preview.__fees, preview.__rules = self.__fees, self.__rules
            preview.__lock = RLock()
            preview.__store, preview.__owns_store = PortfolioLedgerStore(), True
            try:
                preview.__store.save(self.training_id, preview.__state, None)
                order = preview.submit(stock, side, qty, request_id=str(uuid4()))
                return {"version": self.__state.version, "order": order,
                        "note": "按当前收盘估算；买入含价格缓冲，实际按下一开盘重新校验。"}
            finally:
                preview.close()

    @staticmethod
    def _finish_order(order, status, reason):
        order.status, order.reason = status, reason
        order.frozen_cash, order.frozen_shares = ZERO, 0

    def cancel(self, order_id):
        with self.__lock:
            self._require_active()
            state = deepcopy(self.__state)
            order = next((item for item in state.orders if item.order_id == order_id), None)
            if order is None:
                raise ReplayDataError("订单不存在")
            if order.status == "cancelled":
                return serialize(order)
            if order.status != "pending":
                raise ReplayDataError("只有尚未成交的订单可以取消")
            self._finish_order(order, "cancelled", "用户已取消，资金及股数占用已释放")
            self._entry(state, "order_cancelled", reference=order.order_id)
            self._publish(state)
            return serialize(order)

    def advance(self, *, operation_id=None, expected_date):
        """Compare-and-advance; date precondition also stops two DIFFERENT clicks."""
        operation_id = identifier(operation_id if operation_id is not None else str(uuid4()))
        expected_date = day(expected_date)
        with self.__lock:
            self._require_active()
            previous = self.__store.operation(self.training_id, operation_id)
            if previous:
                if previous[0] != str(expected_date):
                    raise ReplayDataError("推进操作标识已用于其他日期")
                return previous[1]
            if self.current_date != expected_date:
                raise ReplayDataError("推进日期已过期，请刷新账户后重试")
            working = self.__replay._engine_fork()
            before = deepcopy(self.__state.daily_assets[-1].marks)
            working.advance()  # ALL data checks finish before any account changes
            state = deepcopy(self.__state)
            state.current_date = working.current_date
            references = self._apply_actions(state, working)
            calendar, securities, _ = working._engine_context()
            for order in state.orders:  # insertion order is execution priority
                if order.status != "pending":
                    continue
                if order.execute_on != state.current_date:
                    raise ReplayDataError("待成交订单执行日期与公共日期不一致")
                quote = working._engine_state(order.stock)
                reason = self.__rules.execution_reason(quote, before[order.stock], order.side,
                                                       securities[order.stock], calendar,
                                                       references.get(order.stock))
                if reason:
                    self._reject(state, order, reason)
                    continue
                self._match(state, order, quote)
            self._record_entitlements(state, working)
            self._mark(state, working, references=references)
            return self._publish(state, working, operation=(operation_id, expected_date))

    def _reject(self, state, order, reason):
        self._finish_order(order, "rejected", reason)
        self._entry(state, "order_rejected", reference=order.order_id, details={"reason": reason})

    def _match(self, state, order, quote):
        gross = money(quote.bar.open * order.quantity)
        fees = self.__fees.calculate(state.current_date, order.side, gross)
        cost, realized, changes = ZERO, ZERO, []
        if order.side == "buy":
            spend = gross + fees["total"]
            other_frozen = self._frozen_cash(state) - order.frozen_cash
            if spend > order.frozen_cash or spend > state.account.cash - other_frozen:
                self._reject(state, order, "下一开盘价含费用超过冻结预算，资金不足；未扩大预算")
                return
            lot = PositionLot(str(uuid4()), order.stock, state.current_date,
                              state.current_date + timedelta(days=1), order.quantity, spend)
            state.lots.append(lot)
            state.account.cash -= spend
            changes.append({"lot_id": lot.lot_id, "quantity": lot.quantity, "cost": spend})
            self._entry(state, "buy", -spend, stock=order.stock, qty=order.quantity,
                        cost=spend, reference=order.order_id, details={"lots": changes})
        else:
            available = sum(lot.quantity for lot in state.lots if lot.stock == order.stock and self._eligible(lot, state.current_date))
            if order.quantity > available:
                self._reject(state, order, "T+1 或股份到账限制：当前可卖股数不足")
                return
            proceeds = gross - fees["total"]
            if state.account.cash + proceeds < self._frozen_cash(state):
                self._reject(state, order, "卖出所得不足以支付费用且可用现金不足")
                return
            remaining = order.quantity
            for lot in state.lots:  # FIFO, keep final remainder exactly in cents
                if lot.stock != order.stock or not self._eligible(lot, state.current_date) or not lot.quantity:
                    continue
                count = min(remaining, lot.quantity)
                released = lot.cost if count == lot.quantity else money(lot.cost * count / lot.quantity)
                lot.quantity -= count
                lot.cost -= released
                cost += released
                remaining -= count
                changes.append({"lot_id": lot.lot_id, "quantity": -count, "cost": -released})
                if not remaining:
                    break
            realized = proceeds - cost
            state.account.cash += proceeds
            state.account.realized_pnl += realized
            self._entry(state, "sell", proceeds, stock=order.stock, qty=-order.quantity,
                        cost=-cost, realized=realized, reference=order.order_id, details={"lots": changes})
        state.fills.append(Fill(str(uuid4()), order.order_id, order.stock, order.side, state.current_date,
                                order.quantity, quote.bar.open, gross, fees, cost, realized,
                                quote.source, quote.data_version, self.__rules.version))
        self._finish_order(order, "filled", "按下一公共交易日开盘价成交（日线简化模拟）")

    def _record_entitlements(self, state, replay):
        _, _, histories = replay._engine_context()
        for code, history in histories.items():
            for event in history.events:
                if event.record_on != state.current_date or event.effective_on > replay._engine_end_date():
                    continue
                key = f"{code}:{event.event_id}"
                holdings = sum(lot.quantity for lot in state.lots if lot.stock == code)
                if event.kind == "bonus_shares" and number(holdings) * event.share_ratio != (number(holdings) * event.share_ratio).to_integral_value():
                    raise ReplayDataError("送转出现不足一股权益，当前不支持尾数分配规则")
                state.entitlements[key] = {
                    "quantity": holdings,
                    "lot_ids": [lot.lot_id for lot in state.lots if lot.stock == code and lot.quantity],
                }
                self._entry(state, "action_record", stock=code, reference=key,
                            details={"entitled_shares": holdings})

    def _apply_actions(self, state, replay):
        _, _, histories = replay._engine_context()
        references = {}
        for code, history in histories.items():
            for event in history.events:
                if event.effective_on != state.current_date:
                    continue
                key = f"{code}:{event.event_id}"
                if key in state.applied_actions:
                    raise ReplayDataError("公司行为重复入账")
                if key not in state.entitlements:
                    raise ReplayDataError("缺少登记日权益，不能核算公司行为")
                if code in references and references[code] != event.reference_price:
                    raise ReplayDataError("同日公司行为除权参考价冲突")
                references[code] = event.reference_price
                # Quantity/price of ex-right pending orders must not silently
                # change. Cancel before matching and require a fresh estimate.
                for order in state.orders:
                    if order.stock == code and order.status == "pending":
                        self._finish_order(order, "cancelled", "公司行为生效，原订单已撤销；请按新股数和价格重新下单")
                        self._entry(state, "order_cancelled", reference=order.order_id,
                                    details={"action": key})
                entitlement = state.entitlements[key]
                entitled = entitlement["quantity"]
                if event.kind == "cash_dividend":
                    cash = money(event.cash_per_share * entitled)
                    state.account.cash += cash
                    state.account.realized_pnl += cash
                    self._entry(state, "cash_dividend", cash, stock=code, realized=cash,
                                reference=key, details={"entitled_shares": entitled, "evidence": event.reference})
                elif event.kind == "bonus_shares":
                    shares = int(number(entitled) * event.share_ratio)
                    changes = []
                    transferred_cost = ZERO
                    # Dilute the basis of retained record-date lots. Entitled
                    # shares already sold keep their recognized sale basis;
                    # their subsequent bonus rights have zero remaining basis.
                    for original in state.lots:
                        if original.lot_id in entitlement["lot_ids"] and original.quantity:
                            allocation = money(original.cost * event.share_ratio / (1 + event.share_ratio))
                            original.cost -= allocation
                            transferred_cost += allocation
                            changes.append({"lot_id": original.lot_id, "quantity": 0, "cost": -allocation})
                    if shares:
                        lot = PositionLot(str(uuid4()), code, event.effective_on - timedelta(days=1),
                                          event.available_on, shares, transferred_cost)
                        state.lots.append(lot)
                        changes.append({"lot_id": lot.lot_id, "quantity": shares, "cost": transferred_cost})
                    self._entry(state, "bonus_shares", stock=code, qty=shares, reference=key,
                                details={"lots": changes, "entitled_shares": entitled, "evidence": event.reference})
                else:
                    raise ReplayDataError("未实现该公司行为核算")
                state.applied_actions.append(key)
        return references

    def _mark(self, state, replay, *, references=None):
        marks = {row["stock"]: {"status": row["status"], **(row["valuation"] or {})}
                 for row in replay.snapshot()["stocks"]}
        for code, mark in marks.items():
            if mark.get("stale"):
                if references and code in references:
                    mark.update(price=str(references[code]), price_date=str(state.current_date),
                                action_reference=True)
                elif state.daily_assets and state.daily_assets[-1].marks[code].get("action_reference"):
                    prior = state.daily_assets[-1].marks[code]
                    mark.update(price=prior["price"], price_date=prior["price_date"], action_reference=True)
        market_value = ZERO
        for code in replay.stocks:
            count = sum(lot.quantity for lot in state.lots if lot.stock == code)
            if count:
                if "price" not in marks[code]:
                    raise ReplayDataError("持仓缺少有效估值价格，不能推进")
                market_value += money(number(marks[code]["price"]) * count)
        cost = sum((lot.cost for lot in state.lots), ZERO)
        assets = state.account.cash + market_value
        previous = state.daily_assets[-1].total_assets if state.daily_assets else state.account.initial_cash
        state.daily_assets.append(DailyAsset(state.current_date, state.account.cash, market_value,
                                            assets, assets - previous, state.account.realized_pnl,
                                            market_value - cost, marks))

    @staticmethod
    def _reconstruct(entries):
        cash, realized = ZERO, ZERO
        positions, costs, lots = {}, {}, {}
        for entry in entries:
            cash += entry.cash_delta
            realized += entry.realized_delta
            if entry.stock:
                positions[entry.stock] = positions.get(entry.stock, 0) + entry.quantity_delta
                costs[entry.stock] = costs.get(entry.stock, ZERO) + entry.cost_delta
            for change in entry.details.get("lots", []):
                old = lots.setdefault(change["lot_id"], {"quantity": 0, "cost": ZERO})
                old["quantity"] += change["quantity"]
                old["cost"] += change["cost"]
        return {"cash": cash, "realized_pnl": realized, "positions": positions, "costs": costs, "lots": lots}

    def _check(self, state):
        book = self._reconstruct(state.ledger)
        for order in state.orders:
            if order.frozen_cash < 0 or order.frozen_shares < 0:
                raise AssertionError("订单冻结资金或股数为负数")
            if order.status != "pending" and (order.frozen_cash or order.frozen_shares):
                raise AssertionError("终态订单仍占用资金或股数")
        if (state.account.cash < 0 or self._frozen_cash(state) > state.account.cash
                or book["cash"] != state.account.cash or book["realized_pnl"] != state.account.realized_pnl):
            raise AssertionError("账户现金或已实现盈亏与账本不一致")
        for lot in state.lots:
            if lot.quantity < 0 or lot.cost < 0 or (lot.quantity == 0 and lot.cost != 0):
                raise AssertionError("持仓数量或成本无效")
            if book["lots"].get(lot.lot_id) != {"quantity": lot.quantity, "cost": lot.cost}:
                raise AssertionError("持仓批次与账本不一致")
        for code in self.__replay.stocks:
            count = sum(lot.quantity for lot in state.lots if lot.stock == code)
            cost = sum((lot.cost for lot in state.lots if lot.stock == code), ZERO)
            if (book["positions"].get(code, 0) != count or book["costs"].get(code, ZERO) != cost
                    or self._frozen_shares(state, code) > count):
                raise AssertionError("持仓或冻结股数与账本不一致")
        for asset in state.daily_assets:
            ledger = [entry for entry in state.ledger if entry.date <= asset.date]
            past = self._reconstruct(ledger)
            market_value = sum((money(number(asset.marks[code]["price"]) * count)
                                for code, count in past["positions"].items() if count), ZERO)
            if (past["cash"] != asset.cash or market_value != asset.market_value
                    or past["cash"] + market_value != asset.total_assets
                    or asset.total_assets - state.account.initial_cash != asset.realized_pnl + asset.unrealized_pnl):
                raise AssertionError("每日资产无法由账本重新计算")

    def reconcile(self):
        with self.__lock:
            self._check(self.__state)
            return serialize(self._reconstruct(self.__state.ledger))

    def _snapshot(self, state, replay):
        result = serialize(state)
        result.update({"training_id": self.training_id, "schema_version": 1,
                       "synthetic": replay.snapshot()["synthetic"],
                       "market_rule_version": self.__rules.version})
        frozen = self._frozen_cash(state)
        result["account"].update(serialize({"frozen_cash": frozen, "available_cash": state.account.cash - frozen}))
        positions = []
        for code in replay.stocks:
            lots = [lot for lot in state.lots if lot.stock == code]
            shares, cost = sum(lot.quantity for lot in lots), sum((lot.cost for lot in lots), ZERO)
            mark = deepcopy(state.daily_assets[-1].marks[code])
            value = money(number(mark["price"]) * shares) if shares else ZERO
            now = sum(lot.quantity for lot in lots if self._eligible(lot, state.current_date))
            try:
                next_date = replay._engine_next_date()
                orderable = self._available_shares(state, code, next_date)
            except ReplayDataError:
                orderable = 0
            positions.append(serialize({"stock": code, "quantity": shares, "cost": cost,
                                        "unit_cost": money(cost / shares) if shares else ZERO,
                                        "market_value": value, "unrealized_pnl": value - cost,
                                        "frozen_shares": self._frozen_shares(state, code),
                                        "sellable_now": max(0, now - self._frozen_shares(state, code)),
                                        "orderable_next_open": orderable, "valuation": mark}))
        result["positions"] = positions
        result["quotes"] = replay.account_data()
        try:
            result["next_date"] = str(replay._engine_next_date())
        except ReplayDataError:
            result["next_date"] = None
        return result

    def snapshot(self):
        with self.__lock:
            return self._snapshot(self.__state, self.__replay)

    def history(self, stock, *, through=None, adjustment="none"):
        with self.__lock:
            return self.__replay.history(stock, through=through, adjustment=adjustment)

    def switch_stock(self, stock):
        with self.__lock:
            return self.__replay.switch_stock(stock)
