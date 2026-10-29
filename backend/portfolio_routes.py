"""Local-profile portfolio HTTP API. No legacy full-data route is reused."""

from functools import wraps
import sqlite3
from urllib.parse import urlsplit

from flask import Blueprint, jsonify, request
from werkzeug.exceptions import BadRequest, UnsupportedMediaType

from backend.portfolio_engine import identifier
from backend.portfolio_repository import PortfolioAPIError
from backend.portfolio_rules import money, quantity
from backend.replay_models import ReplayDataError, day, number, stock_code


COOKIE = "kline_portfolio_profile"


def register_portfolio_routes(app, repository, users, replay_factory):
    api = Blueprint("portfolio", __name__, url_prefix="/api/portfolio")

    @api.before_request
    def same_origin():
        origin = request.headers.get("Origin")
        if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
            raise PortfolioAPIError("origin_denied", "组合接口仅允许当前应用来源访问", 403)
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            raise PortfolioAPIError("origin_denied", "组合接口不接受跨站请求", 403)
        if urlsplit(request.host_url).hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise PortfolioAPIError("local_only", "组合接口仅供本机应用使用", 403)

    def body(required, optional=()):
        value = request.get_json()
        if not isinstance(value, dict) or set(value) - set(required) - set(optional) or set(required) - set(value):
            raise ReplayDataError("请求字段不完整或包含不支持的字段")
        return value

    def profile_identity(user):
        # Existing application has local profiles, no password accounts. Bind to
        # profile creation time too, so deleting/recreating a name cannot inherit
        # another profile's portfolio or its old cookies.
        if not isinstance(user, str) or user not in users.get_users():
            raise PortfolioAPIError("unknown_user", "请先创建并选择现有本地用户", 401)
        config = users.get_user_config(user)
        if not config or not config.get("created_at"):
            raise PortfolioAPIError("unknown_user", "本地用户配置缺少创建标识", 401)
        return user + "\n" + config["created_at"]

    def authenticated(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            owner = repository.session_owner(request.cookies.get(COOKIE))
            if profile_identity(owner.split("\n", 1)[0]) != owner:
                raise PortfolioAPIError("profile_required", "本地用户已改变，请重新选择用户", 401)
            return fn(owner, *args, **kwargs)
        return wrapped

    @api.errorhandler(ReplayDataError)
    def domain_error(exc):
        return jsonify({"error": str(exc), "code": getattr(exc, "code", "invalid_request")}), getattr(exc, "status", 400)

    @api.errorhandler(BadRequest)
    @api.errorhandler(UnsupportedMediaType)
    def malformed_json(exc):
        return jsonify({"error": "请求必须是有效的 JSON 对象", "code": "invalid_json"}), 400

    @api.errorhandler(sqlite3.OperationalError)
    def storage_error(exc):
        app.logger.warning("Portfolio storage unavailable: %s", exc)
        return jsonify({"error": "组合保存暂时失败，事务已回滚；请使用原请求标识重试", "code": "storage_unavailable"}), 503

    @api.after_request
    def private_response(response):
        response.headers["Cache-Control"] = "no-store"
        return response

    @api.post("/session")
    def select_profile():
        data = body(("user",))
        owner = profile_identity(data["user"])
        token = repository.select_profile(owner, request.cookies.get(COOKIE))
        response = jsonify({"user": data["user"], "identity_mode": "local_profile"})
        response.set_cookie(COOKIE, token, max_age=30 * 86400, httponly=True,
                            samesite="Strict", secure=request.is_secure, path="/api/portfolio")
        return response

    @api.get("/trainings")
    @authenticated
    def list_trainings(owner):
        return jsonify({"trainings": repository.list_trainings(owner)})

    @api.post("/trainings")
    @authenticated
    def create(owner):
        data = body(("request_id", "start_date", "end_date"), ("stocks", "initial_cash", "data_source", "mode", "stock_count", "training_days", "sector"))
        identifier(data["request_id"])
        validate_creation(data)
        return jsonify(repository.create(owner, data, replay_factory)), 201

    def validate_creation(data):
        data.setdefault("mode", "specified")
        if data["mode"] not in {"specified", "random"}:
            raise ReplayDataError("请选择指定模式或随机盲盒")
        if data["mode"] == "random":
            data["stocks"] = []
            data.setdefault("stock_count", 3)
            data.setdefault("training_days", 60)
            data.setdefault("sector", "all")
            if type(data["stock_count"]) is not int or not 1 <= data["stock_count"] <= 20:
                raise ReplayDataError("盲盒股票数量须为 1 至 20")
            if type(data["training_days"]) is not int or not 2 <= data["training_days"] <= 1000:
                raise ReplayDataError("盲盒训练时长须为 2 至 1000 个交易日")
            if data["sector"] not in {"all", "main", "gem", "sme"}:
                raise ReplayDataError("不支持的板块筛选")
        elif not isinstance(data.get("stocks"), list) or not 1 <= len(data["stocks"]) <= 20:
            raise ReplayDataError("股票池须包含 1 至 20 个 A 股股票代码")
        data["stocks"] = [stock_code(code) for code in data["stocks"]]
        if len(set(data["stocks"])) != len(data["stocks"]):
            raise ReplayDataError("股票池包含重复股票")
        data["start_date"], data["end_date"] = str(day(data["start_date"])), str(day(data["end_date"]))
        if data["start_date"] > data["end_date"]:
            raise ReplayDataError("结束日期不能早于开始日期")
        initial = number(data.get("initial_cash", "100000"))
        if initial <= 0 or initial > number("1000000000000") or initial != money(initial):
            raise ReplayDataError("初始资金须大于零、不超过一万亿元且最多两位小数")
        data["initial_cash"] = str(money(initial))
        data.setdefault("data_source", "tencent")
        if data["data_source"] not in ("tencent", "akshare", "xtdata", "mootdx", "offline", "sina", "synthetic-demo", "synthetic-practice"):
            raise ReplayDataError("不支持的组合数据源")
        if data["mode"] == "random" and data["data_source"] not in {"tencent", "akshare", "xtdata", "mootdx", "offline"}:
            raise ReplayDataError("随机盲盒请选择与单股训练相同的日线数据源")

    @api.get("/options")
    @authenticated
    def options(owner):
        if set(request.args) - {"q", "data_source"}:
            raise ReplayDataError("不支持的搜索参数")
        return jsonify(replay_factory.options(request.args.get("q", "")[:80],
                                             request.args.get("data_source", "tencent")))

    @api.post("/preview")
    @authenticated
    def preview(owner):
        data = body(("start_date", "end_date"), ("stocks", "initial_cash", "data_source", "mode", "stock_count", "training_days", "sector"))
        validate_creation(data)
        return jsonify(replay_factory.preview(data))

    @api.post("/prepare-data")
    @authenticated
    def prepare_data(owner):
        body(())
        return jsonify(replay_factory.prepare_real_data())

    @api.get("/trainings/<training_id>")
    @authenticated
    def get(owner, training_id):
        return jsonify(repository.get(owner, training_id))

    @api.post("/trainings/<training_id>/restore")
    @authenticated
    def restore(owner, training_id):
        body(())
        return jsonify(repository.get(owner, training_id))

    @api.get("/trainings/<training_id>/history/<stock>")
    @authenticated
    def history(owner, training_id, stock):
        if set(request.args) - {"through", "adjustment"}:
            raise ReplayDataError("行情接口不支持该查询参数")
        return jsonify(repository.history(owner, training_id, stock_code(stock),
            through=request.args.get("through"), adjustment=request.args.get("adjustment", "none")))

    @api.get("/trainings/<training_id>/report")
    @authenticated
    def report(owner, training_id):
        return jsonify(repository.report(owner, training_id))

    def mutation(owner, training_id, action, required=(), optional=()):
        data = body(("request_id", "expected_version", *required), optional)
        identifier(data["request_id"])
        if type(data["expected_version"]) is not int or data["expected_version"] < 0:
            raise ReplayDataError("expected_version 必须是非负整数")
        if action == "submit":
            data["stock"] = stock_code(data["stock"])
            quantity(data["quantity"])
            if data["side"] not in ("buy", "sell"):
                raise ReplayDataError("买卖方向必须为 buy 或 sell")
            if data.get("budget") is not None:
                budget = number(data["budget"])
                if budget <= 0 or budget > number("1000000000000") or budget != money(budget):
                    raise ReplayDataError("买入预算须为不超过一万亿元的正数，最多两位小数")
                data["budget"] = str(money(budget))
        if action == "advance":
            data["expected_date"] = str(day(data["expected_date"]))
        if action == "cancel":
            identifier(data["order_id"])
        return jsonify(repository.mutate(owner, training_id, action, data))

    @api.post("/trainings/<training_id>/orders")
    @authenticated
    def submit(owner, training_id):
        return mutation(owner, training_id, "submit", ("stock", "side", "quantity"), ("budget",))

    @api.post("/trainings/<training_id>/estimate")
    @authenticated
    def estimate(owner, training_id):
        data = body(("stock", "side", "quantity"))
        stock_code(data["stock"])
        quantity(data["quantity"])
        if data["side"] not in ("buy", "sell"):
            raise ReplayDataError("买卖方向必须为 buy 或 sell")
        return jsonify(repository.estimate(owner, training_id, data["stock"], data["side"], data["quantity"]))

    @api.post("/trainings/<training_id>/cancel")
    @authenticated
    def cancel(owner, training_id):
        return mutation(owner, training_id, "cancel", ("order_id",))

    @api.post("/trainings/<training_id>/advance")
    @authenticated
    def advance(owner, training_id):
        return mutation(owner, training_id, "advance", ("expected_date",))

    @api.post("/trainings/<training_id>/end")
    @authenticated
    def end(owner, training_id):
        return mutation(owner, training_id, "end")

    app.register_blueprint(api)
    app.extensions["portfolio_repository"] = repository
