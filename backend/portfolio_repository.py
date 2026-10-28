"""Versioned portfolio storage, owner checks and HTTP idempotency transactions.

Separate database: no migration touches single-stock tables. Stage 3 aggregate
tables remain v1; repository migrations add HTTP metadata around those tables.
"""

from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
from uuid import UUID, uuid4

from backend.portfolio_engine import PortfolioEngine, identifier
from backend.portfolio_ledger_store import PortfolioLedgerStore
from backend.portfolio_models import serialize
from backend.portfolio_replay import PortfolioReplay
from backend.portfolio_rules import FeePeriod, FeeSchedule, MarketRules
from backend.replay_models import ReplayDataError, utc_now
from backend.replay_storage import content_hash


class PortfolioAPIError(ReplayDataError):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.status = code, status


def conflict(message):
    return PortfolioAPIError("conflict", message, 409)


class PortfolioRepository(PortfolioLedgerStore):
    SCHEMA_VERSION = 2

    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        # Check before the base initializer can create or mutate any tables.
        with closing(sqlite3.connect(str(path))) as check:
            version = check.execute("PRAGMA user_version").fetchone()[0]
            if version > self.SCHEMA_VERSION:
                raise PortfolioAPIError("unsupported_schema", "组合数据库版本较新，请升级程序", 503)
        super().__init__(path)
        try:
            self._connection.execute("PRAGMA busy_timeout=5000")
            self._connection.execute("PRAGMA synchronous=FULL")
            with self.transaction():
                version = self._connection.execute("PRAGMA user_version").fetchone()[0]
                if version < 1:
                    self._connection.execute("""CREATE TABLE portfolio_training (
                        training_id TEXT PRIMARY KEY REFERENCES engine_state(training_id),
                        owner TEXT NOT NULL, config TEXT NOT NULL, replay TEXT NOT NULL,
                        replay_hash TEXT NOT NULL, created_at TEXT NOT NULL
                    )""")
                    self._connection.execute("""CREATE TABLE portfolio_request (
                        owner TEXT NOT NULL, scope TEXT NOT NULL, request_id TEXT NOT NULL,
                        fingerprint TEXT NOT NULL, response TEXT NOT NULL,
                        PRIMARY KEY(owner,scope,request_id)
                    )""")
                    self._connection.execute("PRAGMA user_version=1")
                if version < 2:
                    self._connection.execute("""CREATE TABLE portfolio_session (
                        token_hash TEXT PRIMARY KEY, owner TEXT NOT NULL, expires_at TEXT NOT NULL
                    )""")
                    self._connection.execute("PRAGMA user_version=2")
        except BaseException:
            self.close()
            raise

    @contextmanager
    def transaction(self):
        with self._lock:
            db = self._connection
            db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._before_transaction_commit()
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def _before_transaction_commit(self):
        """Test seam: crash after account and request writes, before commit."""

    def select_profile(self, owner, old_token=None):
        token = secrets.token_urlsafe(32)
        expires = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        with self.transaction():
            if old_token:
                self._connection.execute("DELETE FROM portfolio_session WHERE token_hash=?", (self._token_hash(old_token),))
            self._connection.execute("DELETE FROM portfolio_session WHERE expires_at<?", (utc_now(),))
            self._connection.execute("INSERT INTO portfolio_session VALUES(?,?,?)", (self._token_hash(token), owner, expires))
        return token

    @staticmethod
    def _token_hash(token):
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def session_owner(self, token):
        with self._lock:
            row = self._connection.execute("SELECT owner,expires_at FROM portfolio_session WHERE token_hash=?",
                                           (self._token_hash(token or ""),)).fetchone()
        if row is None or datetime.fromisoformat(row[1]) <= datetime.now(timezone.utc):
            raise PortfolioAPIError("profile_required", "请先选择组合训练用户", 401)
        return row[0]

    def _metadata(self, owner, training_id):
        try:
            if str(UUID(training_id)) != training_id:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise PortfolioAPIError("not_found", "组合训练不存在或不属于当前用户", 404)
        row = self._connection.execute("""SELECT config,replay,replay_hash,created_at FROM portfolio_training
            WHERE training_id=? AND owner=?""", (training_id, owner)).fetchone()
        if row is None:
            raise PortfolioAPIError("not_found", "组合训练不存在或不属于当前用户", 404)
        return row

    def _load(self, owner, training_id):
        raw_config, raw_replay, checksum, created = self._metadata(owner, training_id)
        try:
            return self._restore(training_id, raw_config, raw_replay, checksum, created)
        except PortfolioAPIError:
            raise
        except (ValueError, KeyError, TypeError, AssertionError) as exc:
            raise PortfolioAPIError("corrupt_record", "组合记录损坏或核算不一致，禁止继续训练", 503) from exc

    def _restore(self, training_id, raw_config, raw_replay, checksum, created):
        config, payload = json.loads(raw_config), json.loads(raw_replay)
        if payload.get("schema_version") != 1 or content_hash(payload) != checksum:
            raise PortfolioAPIError("corrupt_snapshot", "固定行情快照校验失败，禁止继续训练", 503)
        schema = self._connection.execute("SELECT schema_version,version FROM engine_state WHERE training_id=?", (training_id,)).fetchone()
        if schema is None or schema[0] != 1 or config.get("schema_version") != 1:
            raise PortfolioAPIError("unsupported_schema", "组合记录结构版本不受支持", 503)
        replay = PortfolioReplay._engine_import(payload)
        fees = FeeSchedule([FeePeriod(**item) for item in config["fee_periods"]])
        rules = MarketRules(**config["market_rules"])
        engine = PortfolioEngine.restore(replay, self, training_id, fees=fees, rules=rules)
        if engine.snapshot()["version"] != schema[1]:
            raise PortfolioAPIError("corrupt_record", "组合版本与账户记录不一致", 503)
        return engine, config, created

    @staticmethod
    def _view(engine, config, created):
        return {**engine.snapshot(), "config": config, "created_at": created}

    def get(self, owner, training_id):
        with self.transaction():
            engine, config, created = self._load(owner, training_id)
            return self._view(engine, config, created)

    def list_trainings(self, owner):
        with self._lock:
            rows = self._connection.execute("""SELECT t.training_id,t.config,t.created_at,s.payload
                FROM portfolio_training t JOIN engine_state s USING(training_id)
                WHERE t.owner=? ORDER BY t.created_at DESC,t.training_id""", (owner,)).fetchall()
            return [{"training_id": tid, "config": json.loads(config), "created_at": created,
                     **{key: json.loads(state).get(key, "active" if key == "status" else None)
                        for key in ("status", "current_date", "version")}} for tid, config, created, state in rows]

    def _retry(self, owner, scope, request_id, fingerprint):
        row = self._connection.execute("""SELECT fingerprint,response FROM portfolio_request
            WHERE owner=? AND scope=? AND request_id=?""", (owner, scope, request_id)).fetchone()
        if row:
            if row[0] != fingerprint:
                raise conflict("重复请求标识对应了不同参数或操作")
            return json.loads(row[1])
        return None

    def _remember(self, owner, scope, request_id, fingerprint, response):
        self._connection.execute("INSERT INTO portfolio_request VALUES(?,?,?,?,?)",
                                 (owner, scope, request_id, fingerprint, json.dumps(response, ensure_ascii=False)))

    def create(self, owner, params, replay_factory):
        request_id = identifier(params["request_id"])
        fingerprint = content_hash(params)
        # Network/data preparation runs before the write transaction; only trusted
        # backend factory code may construct evidence or pass synthetic data.
        with self._lock:
            previous = self._retry(owner, "create", request_id, fingerprint)
        if previous is not None:
            return previous
        replay = replay_factory(params)
        if replay.current_date.isoformat() != params["start_date"] or replay._engine_end_date().isoformat() != params["end_date"] or list(replay.stocks) != params["stocks"]:
            raise ReplayDataError("后端数据集与请求的股票池或日期不一致")
        fees, rules = FeeSchedule(), MarketRules()
        pinned = replay._engine_export()
        config = serialize({"schema_version": 1, "stocks": replay.stocks,
            "start_date": params["start_date"], "end_date": params["end_date"],
            "initial_cash": params["initial_cash"], "data_source": params["data_source"],
            "synthetic": pinned["synthetic"], "fee_periods": fees.periods, "market_rules": rules})
        with self.transaction():
            previous = self._retry(owner, "create", request_id, fingerprint)
            if previous is not None:
                return previous
            tid, created = str(uuid4()), utc_now()
            engine = PortfolioEngine(replay, params["initial_cash"], fees=fees, rules=rules, store=self, training_id=tid)
            self._connection.execute("INSERT INTO portfolio_training VALUES(?,?,?,?,?,?)",
                (tid, owner, json.dumps(config), json.dumps(pinned), content_hash(pinned), created))
            result = self._view(engine, config, created)
            self._remember(owner, "create", request_id, fingerprint, result)
            return result

    def mutate(self, owner, training_id, action, params):
        request_id = identifier(params["request_id"])
        fingerprint = content_hash({"action": action, "params": params})
        with self.transaction():
            self._metadata(owner, training_id)  # ownership BEFORE idempotent responses
            previous = self._retry(owner, training_id, request_id, fingerprint)
            if previous is not None:
                return previous
            engine, config, created = self._load(owner, training_id)
            if engine.snapshot()["version"] != params["expected_version"]:
                raise conflict("账户版本已过期，请刷新后使用新的请求标识重试")
            if engine.snapshot()["status"] == "ended":
                raise conflict("组合训练已结束，不能继续交易或推进")
            if action == "submit":
                engine.submit(params["stock"], params["side"], params["quantity"], request_id=request_id, budget=params.get("budget"))
            elif action == "cancel":
                engine.cancel(params["order_id"])
            elif action == "advance":
                if str(engine.current_date) != params["expected_date"]:
                    raise conflict("推进日期已过期，请刷新账户后重试")
                engine.advance(operation_id=request_id, expected_date=params["expected_date"])
            elif action == "end":
                engine.end()
            else:
                raise ReplayDataError("未知组合操作")
            result = self._view(engine, config, created)
            if action == "end":
                result["report"] = engine.report()
            self._remember(owner, training_id, request_id, fingerprint, result)
            return result

    def history(self, owner, training_id, stock, **kwargs):
        with self.transaction():
            engine, _, _ = self._load(owner, training_id)
            return {"current_date": str(engine.current_date), "stock": stock,
                    "bars": engine.history(stock, **kwargs)}

    def estimate(self, owner, training_id, stock, side, qty):
        with self.transaction():
            engine, _, _ = self._load(owner, training_id)
            return engine.estimate(stock, side, qty)

    def report(self, owner, training_id):
        with self.transaction():
            engine, _, _ = self._load(owner, training_id)
            if engine.snapshot()["status"] != "ended":
                raise conflict("请先结束组合训练再查看完整报告")
            return engine.report()
