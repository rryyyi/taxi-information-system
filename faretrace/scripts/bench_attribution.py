"""归因计算基准（Dream-RSI 工作负载声明）。

在独立临时库中灌入 7 笔种子订单，然后对每笔订单重复执行 run_attribution
（纯计算，不落库），输出总耗时（毫秒，stdout 单个数字）。

用法：python scripts/bench_attribution.py [rounds]   # 默认 rounds=500
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_tmp = tempfile.mkdtemp(prefix="faretrace-bench-")
os.environ["FARETRACE_DB_URL"] = "sqlite:///" + (_tmp + "/bench.db").replace("\\", "/")
os.environ["FARETRACE_SEED_ON_START"] = "0"
os.environ["FARETRACE_CONFIG"] = str(ROOT / "config" / "attribution.yaml")

from sqlalchemy import select  # noqa: E402

from app.config import get_config  # noqa: E402
from app.db import SessionLocal, reset_db  # noqa: E402
from app.models import Order  # noqa: E402
from app.services.attribution import run_attribution  # noqa: E402
from app.services.seed import seed_orders  # noqa: E402


def main() -> int:
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 500
    cfg = get_config()
    reset_db()
    with SessionLocal() as session:
        seed_orders(session)
        session.commit()
        orders = session.scalars(select(Order)).all()
        # 预热一次（消除首次导入/建表影响），并验证结果可复现
        baseline = {
            order.order_no: [str(i.amount) for i in run_attribution(session, order, cfg).items]
            for order in orders
        }
        for order in orders:
            sig = [str(i.amount) for i in run_attribution(session, order, cfg).items]
            if baseline[order.order_no] != sig:
                print("NONDETERMINISTIC", file=sys.stderr)
                return 2
        # 重复 3 次取最小值：对系统噪声最稳健的点估计，保证 evaluator 读数稳定
        readings: list[float] = []
        for _ in range(3):
            t0 = time.perf_counter()
            for _ in range(rounds):
                for order in orders:
                    run_attribution(session, order, cfg)
            readings.append((time.perf_counter() - t0) * 1000.0)
        elapsed_ms = min(readings)
    print(f"{elapsed_ms:.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
