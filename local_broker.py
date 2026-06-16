"""
local_broker.py — 本地虚拟撮合引擎

以真实 moomoo 行情价格为基准，在本地模拟买卖、持仓、盈亏。
不调用任何交易 API，完全无风险。模拟万三手续费（最低 $1）。
"""
from __future__ import annotations
import json, os, csv, sqlite3, threading
from datetime import datetime

from trade_costs import calc_commission


class LocalBroker:
    def __init__(self, db_path: str, log_path: str,
                 initial_cash: float = 1_000_000.0):
        self.db_path = db_path
        self.snapshot_path = db_path
        self.sqlite_path = self._derive_sqlite_path(db_path)
        self.log_path = log_path
        self._lock = threading.Lock()
        self._initial_cash = float(initial_cash)

        self._init_storage()

    # ── 内部 IO ────────────────────────────────────────────
    def _derive_sqlite_path(self, path: str) -> str:
        root, ext = os.path.splitext(path)
        if ext.lower() in {'.sqlite', '.sqlite3', '.db'}:
            return path
        return f"{root}.sqlite3"

    def _default_state(self, initial_cash: float | None = None) -> dict:
        base_cash = float(self._initial_cash if initial_cash is None else initial_cash)
        return {
            'initial_cash': base_cash,
            'cash': base_cash,
            'reserved_cash': 0.0,
            'realized_pnl': 0.0,
            'total_commission': 0.0,
            'positions': {},
            'orders': [],
            'fills': [],
            'next_order_id': 1,
            'meta': {
                'markers': {},
            },
        }

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.sqlite_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection):
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS account_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS positions (
            code TEXT PRIMARY KEY,
            payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY,
            seq INTEGER NOT NULL UNIQUE,
            payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fills (
            fill_id TEXT PRIMARY KEY,
            order_id TEXT NOT NULL,
            seq INTEGER NOT NULL UNIQUE,
            payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS markers (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """)
        conn.commit()

    def _has_sqlite_state(self, conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT 1 FROM account_meta WHERE key = 'next_order_id' LIMIT 1"
        ).fetchone()
        return row is not None

    def _load_snapshot_file(self) -> dict | None:
        if self.snapshot_path == self.sqlite_path:
            return None
        if not os.path.exists(self.snapshot_path):
            return None
        try:
            with open(self.snapshot_path) as f:
                return self._normalize_state(json.load(f))
        except Exception:
            return None

    def _write_snapshot_file(self, state: dict):
        if self.snapshot_path == self.sqlite_path:
            return
        with open(self.snapshot_path, 'w') as f:
            json.dump(state, f, indent=2, ensure_ascii=False)

    def _sqlite_rows_to_state(self, conn: sqlite3.Connection) -> dict:
        state = self._default_state()
        meta = {
            row['key']: row['value']
            for row in conn.execute("SELECT key, value FROM account_meta")
        }
        state['initial_cash'] = float(meta.get('initial_cash', state['initial_cash']))
        state['cash'] = float(meta.get('cash', state['cash']))
        state['reserved_cash'] = float(meta.get('reserved_cash', state['reserved_cash']))
        state['realized_pnl'] = float(meta.get('realized_pnl', state['realized_pnl']))
        state['total_commission'] = float(meta.get('total_commission', state['total_commission']))
        state['next_order_id'] = int(meta.get('next_order_id', state['next_order_id']))
        state['positions'] = {
            str(row['code']): json.loads(row['payload'])
            for row in conn.execute("SELECT code, payload FROM positions ORDER BY code")
        }
        state['orders'] = [
            json.loads(row['payload'])
            for row in conn.execute("SELECT payload FROM orders ORDER BY seq")
        ]
        state['fills'] = [
            json.loads(row['payload'])
            for row in conn.execute("SELECT payload FROM fills ORDER BY seq")
        ]
        state['meta'] = {
            'markers': {
                str(row['key']): str(row['value'])
                for row in conn.execute("SELECT key, value FROM markers ORDER BY key")
            }
        }
        return self._normalize_state(state)

    def _write_sqlite(self, state: dict):
        with self._connect() as conn:
            self._ensure_schema(conn)
            conn.execute("BEGIN")
            conn.execute("DELETE FROM account_meta")
            conn.execute("DELETE FROM positions")
            conn.execute("DELETE FROM orders")
            conn.execute("DELETE FROM fills")
            conn.execute("DELETE FROM markers")
            conn.executemany(
                "INSERT INTO account_meta(key, value) VALUES(?, ?)",
                [
                    ('initial_cash', str(state['initial_cash'])),
                    ('cash', str(state['cash'])),
                    ('reserved_cash', str(state.get('reserved_cash', 0.0))),
                    ('realized_pnl', str(state['realized_pnl'])),
                    ('total_commission', str(state['total_commission'])),
                    ('next_order_id', str(state['next_order_id'])),
                ],
            )
            conn.executemany(
                "INSERT INTO positions(code, payload) VALUES(?, ?)",
                [
                    (str(code), json.dumps(payload, ensure_ascii=False))
                    for code, payload in state['positions'].items()
                ],
            )
            conn.executemany(
                "INSERT INTO orders(order_id, seq, payload) VALUES(?, ?, ?)",
                [
                    (
                        str(order.get('order_id', '')),
                        idx,
                        json.dumps(order, ensure_ascii=False),
                    )
                    for idx, order in enumerate(state['orders'], start=1)
                ],
            )
            conn.executemany(
                "INSERT INTO fills(fill_id, order_id, seq, payload) VALUES(?, ?, ?, ?)",
                [
                    (
                        str(fill.get('fill_id', '')),
                        str(fill.get('order_id', '')),
                        idx,
                        json.dumps(fill, ensure_ascii=False),
                    )
                    for idx, fill in enumerate(state['fills'], start=1)
                ],
            )
            conn.executemany(
                "INSERT INTO markers(key, value) VALUES(?, ?)",
                [
                    (str(key), str(value))
                    for key, value in state.get('meta', {}).get('markers', {}).items()
                ],
            )
            conn.commit()

    def _init_storage(self):
        os.makedirs(os.path.dirname(self.sqlite_path) or '.', exist_ok=True)
        with self._connect() as conn:
            self._ensure_schema(conn)
            if self._has_sqlite_state(conn):
                state = self._sqlite_rows_to_state(conn)
                self._write_snapshot_file(state)
                return

        seed = self._load_snapshot_file()
        state = seed if seed is not None else self._default_state(self._initial_cash)
        self._write_sqlite(state)
        self._write_snapshot_file(state)

    def _normalize_state(self, state: dict) -> dict:
        state.setdefault('initial_cash', 1_000_000.0)
        state.setdefault('cash', state['initial_cash'])
        state.setdefault('reserved_cash', 0.0)
        state.setdefault('realized_pnl', 0.0)
        state.setdefault('total_commission', 0.0)
        state.setdefault('positions', {})
        state.setdefault('orders', [])
        state.setdefault('fills', [])
        state.setdefault('next_order_id', 1)
        state.setdefault('meta', {})
        state['meta'].setdefault('markers', {})

        for pos in state['positions'].values():
            pos['reserved_qty'] = 0
        for pos in state['positions'].values():
            pos.setdefault('add_count', 0)
            pos.setdefault('profit_stages', [])
            pos.setdefault('trail_high', pos.get('avg_cost', 0.0))

        reserved_cash = 0.0
        for order in state['orders']:
            order['requested_qty'] = int(order.get('requested_qty', 0) or 0)
            order['requested_price'] = round(float(order.get('requested_price', 0.0) or 0.0), 6)
            order['filled_qty'] = int(order.get('filled_qty', 0) or 0)
            avg_fill_price = order.get('avg_fill_price')
            order['avg_fill_price'] = (
                None if avg_fill_price in (None, '')
                else round(float(avg_fill_price), 6)
            )
            order['commission'] = round(float(order.get('commission', 0.0) or 0.0), 6)
            order.setdefault('status', 'NEW')
            order.setdefault('submitted_at', '')
            order.setdefault('updated_at', order.get('submitted_at', ''))
            order.setdefault('message', '')
            order['fill_count'] = int(
                order.get('fill_count', 1 if order['filled_qty'] > 0 else 0) or 0
            )
            order['reserved_cash'] = round(float(order.get('reserved_cash', 0.0) or 0.0), 6)
            order['reserved_qty'] = int(order.get('reserved_qty', 0) or 0)
            order['position_add_applied'] = bool(order.get('position_add_applied', order['filled_qty'] > 0))
            order['remaining_qty'] = max(0, order['requested_qty'] - order['filled_qty'])
            if order['status'] in {'NEW', 'PARTIALLY_FILLED'}:
                side = str(order.get('side', '') or '').upper()
                if side == 'BUY':
                    reserved_cash += order['reserved_cash']
                elif side == 'SELL':
                    code = str(order.get('code', '') or '')
                    if code in state['positions']:
                        state['positions'][code]['reserved_qty'] += order['reserved_qty']
        state['reserved_cash'] = round(reserved_cash, 6)
        return state

    def _read(self) -> dict:
        with self._connect() as conn:
            self._ensure_schema(conn)
            if self._has_sqlite_state(conn):
                return self._sqlite_rows_to_state(conn)
        seed = self._load_snapshot_file()
        return seed if seed is not None else self._default_state(self._initial_cash)

    def _write(self, state: dict):
        normalized = self._normalize_state(state)
        self._write_sqlite(normalized)
        self._write_snapshot_file(normalized)

    def _log(self, time_key, code, bucket, side, price, qty, reason, pnl=None):
        exists = os.path.exists(self.log_path)
        with open(self.log_path, 'a', newline='') as f:
            w = csv.writer(f)
            if not exists:
                w.writerow(['time','stock','bucket','side','price','qty','reason','pnl'])
            w.writerow([time_key, code, bucket, side,
                        f'{price:.4f}', qty, reason,
                        f'{pnl:.2f}' if pnl is not None else ''])

    def _parse_marker_time(self, value: str) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
        except ValueError:
            return None

    def _alloc_order_id(self, state: dict) -> str:
        next_id = int(state.get('next_order_id', 1) or 1)
        state['next_order_id'] = next_id + 1
        return f"ORD{next_id:08d}"

    def _append_order(self,
                      state: dict,
                      *,
                      order_id: str,
                      code: str,
                      side: str,
                      qty: int,
                      price: float,
                      bucket: str,
                      reason: str,
                      submitted_at: str) -> dict:
        order = {
            'order_id': order_id,
            'code': code,
            'side': side,
            'bucket': bucket,
            'reason': reason,
            'requested_qty': int(qty),
            'requested_price': round(float(price), 6),
            'filled_qty': 0,
            'remaining_qty': int(qty),
            'avg_fill_price': None,
            'commission': 0.0,
            'fill_count': 0,
            'reserved_cash': 0.0,
            'reserved_qty': 0,
            'position_add_applied': False,
            'status': 'NEW',
            'submitted_at': submitted_at,
            'updated_at': submitted_at,
            'message': '',
        }
        state['orders'].append(order)
        return order

    def _estimate_buy_reservation(self, price: float, qty: int) -> float:
        if qty <= 0 or price <= 0:
            return 0.0
        return round(float(price) * int(qty) + calc_commission(float(price), int(qty)), 6)

    def _find_order(self, state: dict, order_id: str) -> dict | None:
        for order in state.get('orders', []):
            if order.get('order_id') == order_id:
                return order
        return None

    def _record_fill(self,
                     state: dict,
                     order: dict,
                     *,
                     code: str,
                     side: str,
                     qty: int,
                     price: float,
                     bucket: str,
                     reason: str,
                     commission: float,
                     fill_time: str,
                     message: str = 'filled',
                     pnl: float | None = None):
        prev_qty = int(order.get('filled_qty', 0) or 0)
        prev_avg = order.get('avg_fill_price')
        total_qty = prev_qty + int(qty)
        order['fill_count'] = int(order.get('fill_count', 0) or 0) + 1
        order['filled_qty'] = total_qty
        order['remaining_qty'] = max(0, int(order.get('requested_qty', 0) or 0) - total_qty)
        if prev_avg in (None, '') or prev_qty <= 0:
            avg_fill = float(price)
        else:
            avg_fill = ((float(prev_avg) * prev_qty) + (float(price) * int(qty))) / total_qty
        order['avg_fill_price'] = round(avg_fill, 6)
        order['commission'] = round(float(order.get('commission', 0.0) or 0.0) + float(commission), 6)
        order['status'] = 'FILLED' if order['remaining_qty'] == 0 else 'PARTIALLY_FILLED'
        order['updated_at'] = fill_time
        order['message'] = message
        state['fills'].append({
            'order_id': order['order_id'],
            'fill_id': f"{order['order_id']}-F{order['fill_count']}",
            'code': code,
            'side': side,
            'bucket': bucket,
            'reason': reason,
            'qty': int(qty),
            'price': round(float(price), 6),
            'commission': round(float(commission), 6),
            'time': fill_time,
            'pnl': None if pnl is None else round(float(pnl), 6),
        })

    def _reject_order(self, order: dict, *, rejected_at: str, message: str):
        order['status'] = 'REJECTED'
        order['remaining_qty'] = max(0, int(order.get('requested_qty', 0) or 0) - int(order.get('filled_qty', 0) or 0))
        order['updated_at'] = rejected_at
        order['message'] = message

    def _cancel_order(self, state: dict, order: dict, *, canceled_at: str, message: str):
        side = str(order.get('side', '') or '').upper()
        if side == 'BUY':
            release_cash = float(order.get('reserved_cash', 0.0) or 0.0)
            state['reserved_cash'] = round(max(0.0, float(state.get('reserved_cash', 0.0) or 0.0) - release_cash), 6)
            order['reserved_cash'] = 0.0
        elif side == 'SELL':
            code = str(order.get('code', '') or '')
            release_qty = int(order.get('reserved_qty', 0) or 0)
            if code in state['positions']:
                pos = state['positions'][code]
                pos['reserved_qty'] = max(0, int(pos.get('reserved_qty', 0) or 0) - release_qty)
            order['reserved_qty'] = 0
        order['status'] = 'CANCELED'
        order['updated_at'] = canceled_at
        order['message'] = message

    def _submit_order_locked(
        self,
        state: dict,
        *,
        code: str,
        side: str,
        qty: int,
        price: float,
        bucket: str,
        reason: str,
        submitted_at: str,
        allow_sell_clip: bool = False,
    ) -> tuple[bool, str, dict]:
        side = str(side or '').upper()
        order_id = self._alloc_order_id(state)
        order = self._append_order(
            state,
            order_id=order_id,
            code=code,
            side=side,
            qty=qty,
            price=price,
            bucket=bucket,
            reason=reason,
            submitted_at=submitted_at,
        )

        if side not in {'BUY', 'SELL'}:
            msg = f"未知方向 side={side}"
            self._reject_order(order, rejected_at=submitted_at, message=msg)
            return False, msg, order

        if qty <= 0 or price <= 0:
            msg = f"无效参数 qty={qty} price={price}"
            self._reject_order(order, rejected_at=submitted_at, message=msg)
            return False, msg, order

        if side == 'BUY':
            reserved_cash = self._estimate_buy_reservation(price, qty)
            available_cash = float(state.get('cash', 0.0) or 0.0) - float(state.get('reserved_cash', 0.0) or 0.0)
            if available_cash < reserved_cash:
                msg = (f"现金不足 ${available_cash:,.0f}，"
                       f"需要 ${reserved_cash:,.0f}")
                self._reject_order(order, rejected_at=submitted_at, message=msg)
                return False, msg, order
            order['reserved_cash'] = reserved_cash
            state['reserved_cash'] = round(float(state.get('reserved_cash', 0.0) or 0.0) + reserved_cash, 6)
            order['message'] = 'accepted'
            return True, 'accepted', order

        pos = state['positions'].get(code)
        if pos is None:
            msg = f"无 {code} 持仓"
            self._reject_order(order, rejected_at=submitted_at, message=msg)
            return False, msg, order
        available_qty = max(0, int(pos.get('qty', 0) or 0) - int(pos.get('reserved_qty', 0) or 0))
        if not allow_sell_clip and qty > available_qty:
            msg = f"{code} 持仓不足 {qty}股"
            self._reject_order(order, rejected_at=submitted_at, message=msg)
            return False, msg, order
        reserve_qty = available_qty if allow_sell_clip else min(qty, available_qty)
        if reserve_qty <= 0:
            msg = f"{code} 无可卖数量"
            self._reject_order(order, rejected_at=submitted_at, message=msg)
            return False, msg, order
        order['reserved_qty'] = reserve_qty
        pos['reserved_qty'] = int(pos.get('reserved_qty', 0) or 0) + reserve_qty
        if reserve_qty < qty:
            order['message'] = f'accepted_clipped:{reserve_qty}/{qty}'
            return True, order['message'], order
        order['message'] = 'accepted'
        return True, 'accepted', order

    def _apply_buy_fill(
        self,
        state: dict,
        *,
        code: str,
        qty: int,
        price: float,
        bucket: str,
        fill_time: str,
        commission: float,
        increment_add_count: bool,
    ) -> tuple[bool, str]:
        total_cost = price * qty + commission
        if state['cash'] < total_cost:
            return False, (f"现金不足 ${state['cash']:,.0f}，"
                           f"需要 ${total_cost:,.0f}")

        state['cash'] -= total_cost
        state['total_commission'] += commission

        pos = state['positions']
        if code in pos:
            old = pos[code]
            new_qty = int(old.get('qty', 0) or 0) + qty
            avg_cost = ((int(old.get('qty', 0) or 0) * float(old.get('avg_cost', 0.0) or 0.0)) + qty * price) / new_qty
            old['qty'] = new_qty
            old['avg_cost'] = round(avg_cost, 6)
            if increment_add_count:
                old['add_count'] = int(old.get('add_count', 0) or 0) + 1
        else:
            pos[code] = {
                'qty': qty,
                'avg_cost': price,
                'bucket': bucket,
                'entry_time': fill_time,
                'add_count': 0,
                'profit_stages': [],
                'trail_high': price,
                'reserved_qty': 0,
            }
        return True, 'filled'

    def _apply_sell_fill(
        self,
        state: dict,
        *,
        code: str,
        requested_qty: int,
        price: float,
        reason: str,
        fill_time: str,
    ) -> tuple[bool, str, int, str, float | None]:
        pos = state['positions']
        if code not in pos:
            return False, f"无 {code} 持仓", 0, '', None

        p = pos[code]
        available_qty = int(p.get('qty', 0) or 0)
        sell_qty = min(int(requested_qty), available_qty)
        if sell_qty <= 0:
            return False, f"{code} 可卖数量为0", 0, str(p.get('bucket', '') or ''), None

        actual_commission = calc_commission(price, sell_qty)
        pnl = (price - float(p.get('avg_cost', 0.0) or 0.0)) * sell_qty - actual_commission

        state['cash'] += price * sell_qty - actual_commission
        state['realized_pnl'] += pnl
        state['total_commission'] += actual_commission

        if sell_qty >= available_qty:
            del pos[code]
        else:
            p['qty'] = available_qty - sell_qty

        markers = state.setdefault('meta', {}).setdefault('markers', {})
        markers[f'last_sell_ts:{code}'] = fill_time
        markers[f'last_sell_reason:{code}'] = reason

        return True, ('filled' if sell_qty == requested_qty else f'filled_clipped:{sell_qty}/{requested_qty}'), sell_qty, str(p.get('bucket', '') or ''), pnl

    def _fill_order_locked(
        self,
        state: dict,
        *,
        order_id: str,
        qty: int | None = None,
        price: float | None = None,
        fill_time: str,
    ) -> tuple[bool, str, dict | None]:
        order = self._find_order(state, order_id)
        if order is None:
            return False, f"无订单 {order_id}", None

        if order.get('status') in {'FILLED', 'REJECTED', 'CANCELED'}:
            return False, f"订单 {order_id} 状态={order.get('status')}", order

        remaining_qty = int(order.get('remaining_qty', 0) or 0)
        if remaining_qty <= 0:
            return False, f"订单 {order_id} 无剩余数量", order

        requested_fill_qty = remaining_qty if qty is None else int(qty)
        if requested_fill_qty <= 0:
            return False, f"无效成交数量 qty={requested_fill_qty}", order
        fill_qty = min(requested_fill_qty, remaining_qty)

        exec_price = float(price if price is not None else order.get('requested_price', 0.0) or 0.0)
        if exec_price <= 0:
            return False, f"无效成交价格 price={exec_price}", order

        side = str(order.get('side', '') or '').upper()
        code = str(order.get('code', '') or '')
        bucket = str(order.get('bucket', '') or '')
        reason = str(order.get('reason', '') or '')

        if side == 'BUY':
            old_reserved = float(order.get('reserved_cash', 0.0) or 0.0)
            projected_remaining = max(0, remaining_qty - fill_qty)
            new_reserved = self._estimate_buy_reservation(
                float(order.get('requested_price', 0.0) or 0.0),
                projected_remaining,
            )
            commission = calc_commission(exec_price, fill_qty)
            actual_total_cost = exec_price * fill_qty + commission
            other_reserved = max(0.0, float(state.get('reserved_cash', 0.0) or 0.0) - old_reserved)
            if float(state.get('cash', 0.0) or 0.0) - other_reserved < actual_total_cost + new_reserved:
                msg = (f"现金不足，需覆盖成交 ${actual_total_cost:,.2f}"
                       f" 与剩余预留 ${new_reserved:,.2f}")
                if int(order.get('filled_qty', 0) or 0) <= 0:
                    self._reject_order(order, rejected_at=fill_time, message=msg)
                    state['reserved_cash'] = round(other_reserved, 6)
                    order['reserved_cash'] = 0.0
                else:
                    order['updated_at'] = fill_time
                    order['message'] = msg
                return False, msg, order

            existed_before = code in state['positions'] and int(state['positions'][code].get('qty', 0) or 0) > 0
            ok, msg = self._apply_buy_fill(
                state,
                code=code,
                qty=fill_qty,
                price=exec_price,
                bucket=bucket,
                fill_time=fill_time,
                commission=commission,
                increment_add_count=(existed_before and not bool(order.get('position_add_applied', False))),
            )
            if not ok:
                if int(order.get('filled_qty', 0) or 0) <= 0:
                    self._reject_order(order, rejected_at=fill_time, message=msg)
                else:
                    order['updated_at'] = fill_time
                    order['message'] = msg
                return False, msg, order

            state['reserved_cash'] = round(other_reserved + new_reserved, 6)
            order['reserved_cash'] = round(new_reserved, 6)
            order['position_add_applied'] = True
            self._record_fill(
                state, order,
                code=code, side='BUY', qty=fill_qty, price=exec_price,
                bucket=bucket, reason=reason, commission=commission,
                fill_time=fill_time, message=('filled' if projected_remaining == 0 else f'partially_filled:{fill_qty}/{remaining_qty}'),
            )
            self._log(fill_time, code, bucket, 'BUY', exec_price, fill_qty, reason)
            return True, f"买入 {fill_qty}股 @ ${exec_price:.2f}  手续费 ${commission:.2f}", order

        if side == 'SELL':
            reserved_qty = int(order.get('reserved_qty', 0) or 0)
            if reserved_qty <= 0:
                msg = f"订单 {order_id} 无可卖锁定数量"
                if int(order.get('filled_qty', 0) or 0) <= 0:
                    self._reject_order(order, rejected_at=fill_time, message=msg)
                else:
                    order['updated_at'] = fill_time
                    order['message'] = msg
                return False, msg, order
            sellable_qty = min(fill_qty, reserved_qty)
            ok, fill_tag, actual_qty, actual_bucket, pnl = self._apply_sell_fill(
                state,
                code=code,
                requested_qty=sellable_qty,
                price=exec_price,
                reason=reason,
                fill_time=fill_time,
            )
            if not ok:
                if int(order.get('filled_qty', 0) or 0) <= 0:
                    self._reject_order(order, rejected_at=fill_time, message=fill_tag)
                else:
                    order['updated_at'] = fill_time
                    order['message'] = fill_tag
                return False, fill_tag, order

            order['reserved_qty'] = max(0, reserved_qty - actual_qty)
            if code in state['positions']:
                pos = state['positions'][code]
                pos['reserved_qty'] = max(0, int(pos.get('reserved_qty', 0) or 0) - actual_qty)
            actual_commission = calc_commission(exec_price, actual_qty)
            self._record_fill(
                state, order,
                code=code, side='SELL', qty=actual_qty, price=exec_price,
                bucket=actual_bucket or bucket, reason=reason,
                commission=actual_commission, fill_time=fill_time,
                message=fill_tag, pnl=pnl,
            )
            self._log(fill_time, code, actual_bucket or bucket, 'SELL',
                      exec_price, actual_qty, reason, pnl)
            return True, (f"卖出 {actual_qty}股 @ ${exec_price:.2f}"
                          f"  盈亏 ${float(pnl or 0.0):+.2f}  手续费 ${actual_commission:.2f}"), order

        msg = f"未知方向 side={side}"
        self._reject_order(order, rejected_at=fill_time, message=msg)
        return False, msg, order

    # ── 下单 ───────────────────────────────────────────────
    def place_order(self, code: str, side: str, qty: int, price: float,
                    bucket: str = '', reason: str = '') -> tuple[bool, str]:
        """
        side: 'BUY' | 'SELL'
        Returns (success, message)
        """
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        with self._lock:
            s = self._read()
            ok, msg, order = self._submit_order_locked(
                s,
                code=code,
                side=side,
                qty=qty,
                price=price,
                bucket=bucket,
                reason=reason,
                submitted_at=now,
                allow_sell_clip=True,
            )
            if not ok:
                self._write(s)
                return False, msg

            ok, msg, order = self._fill_order_locked(
                s,
                order_id=str(order.get('order_id', '') or ''),
                fill_time=now,
            )
            if ok and order and int(order.get('remaining_qty', 0) or 0) > 0:
                self._cancel_order(
                    s,
                    order,
                    canceled_at=now,
                    message=(f"auto_canceled_remainder:"
                             f"{int(order.get('filled_qty', 0) or 0)}/"
                             f"{int(order.get('requested_qty', 0) or 0)}"),
                )
            self._write(s)
            return ok, msg

        return False, "未知错误"

    def submit_order(self, code: str, side: str, qty: int, price: float,
                     bucket: str = '', reason: str = '') -> tuple[bool, str, dict | None]:
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with self._lock:
            s = self._read()
            ok, msg, order = self._submit_order_locked(
                s,
                code=code,
                side=side,
                qty=qty,
                price=price,
                bucket=bucket,
                reason=reason,
                submitted_at=now,
            )
            self._write(s)
            return ok, msg, dict(order)

    def fill_order(self, order_id: str, qty: int | None = None,
                   price: float | None = None) -> tuple[bool, str]:
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with self._lock:
            s = self._read()
            ok, msg, _ = self._fill_order_locked(
                s,
                order_id=order_id,
                qty=qty,
                price=price,
                fill_time=now,
            )
            self._write(s)
            return ok, msg

    def cancel_order(self, order_id: str, message: str = 'canceled') -> tuple[bool, str]:
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with self._lock:
            s = self._read()
            order = self._find_order(s, order_id)
            if order is None:
                return False, f"无订单 {order_id}"
            if order.get('status') in {'FILLED', 'REJECTED', 'CANCELED'}:
                return False, f"订单 {order_id} 状态={order.get('status')}"
            self._cancel_order(s, order, canceled_at=now, message=message)
            self._write(s)
            return True, f"已撤销 {order_id}"

    # ── 查询 ───────────────────────────────────────────────
    def get_state(self) -> dict:
        with self._lock:
            return self._read()

    def get_cash(self) -> float:
        return self.get_state()['cash']

    def get_available_cash(self) -> float:
        state = self.get_state()
        return round(
            float(state.get('cash', 0.0) or 0.0) -
            float(state.get('reserved_cash', 0.0) or 0.0),
            6,
        )

    def get_available_qty(self, code: str) -> int:
        state = self.get_state()
        pos = state.get('positions', {}).get(code, {})
        return max(
            0,
            int(pos.get('qty', 0) or 0) - int(pos.get('reserved_qty', 0) or 0),
        )

    def get_order(self, order_id: str) -> dict | None:
        orders = self.get_orders()
        for order in orders:
            if order.get('order_id') == order_id:
                return order
        return None

    def get_orders(self, limit: int | None = None) -> list[dict]:
        orders = list(self.get_state().get('orders', []))
        if limit is None or limit <= 0:
            return orders
        return orders[-limit:]

    def get_fills(self, limit: int | None = None) -> list[dict]:
        fills = list(self.get_state().get('fills', []))
        if limit is None or limit <= 0:
            return fills
        return fills[-limit:]

    def get_marker(self, key: str, default: str = '') -> str:
        s = self.get_state()
        return str(s.get('meta', {}).get('markers', {}).get(key, default))

    def set_marker(self, key: str, value: str):
        with self._lock:
            s = self._read()
            s.setdefault('meta', {}).setdefault('markers', {})[key] = value
            self._write(s)

    def was_sold_recently(
        self,
        code: str,
        cooldown_minutes: int,
        now: datetime | None = None,
    ) -> bool:
        if cooldown_minutes <= 0:
            return False
        marker = self.get_marker(f'last_sell_ts:{code}', '')
        sold_at = self._parse_marker_time(marker)
        if sold_at is None:
            return False
        current = now or datetime.now()
        age_seconds = (current - sold_at).total_seconds()
        return 0 <= age_seconds < cooldown_minutes * 60

    def last_sell_reason(self, code: str) -> str:
        return self.get_marker(f'last_sell_reason:{code}', '')

    def update_trail_high(self, code: str, high: float):
        """更新某只股票的移动止损高水位（持久化）。"""
        with self._lock:
            s = self._read()
            if code in s['positions']:
                s['positions'][code]['trail_high'] = round(high, 4)
                self._write(s)

    def update_profit_stages(self, code: str, stages: set):
        """更新某只股票的已触发止盈阶段（持久化）。"""
        with self._lock:
            s = self._read()
            if code in s['positions']:
                s['positions'][code]['profit_stages'] = sorted(stages)
                self._write(s)

    def get_trail_high(self, code: str, default: float = 0.0) -> float:
        s = self.get_state()
        return float(s['positions'].get(code, {}).get('trail_high', default))

    def get_profit_stages(self, code: str) -> set:
        s = self.get_state()
        return set(s['positions'].get(code, {}).get('profit_stages', []))

    def total_assets(self, price_map: dict[str, float]) -> float:
        """传入 {code: last_price} 计算总资产。"""
        s = self.get_state()
        mkt_val = sum(
            p['qty'] * price_map.get(code, p['avg_cost'])
            for code, p in s['positions'].items()
        )
        return s['cash'] + mkt_val
