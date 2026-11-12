"""种子脚本入口：`python seed.py`

重置数据库并写入 6 笔合成演示订单（含归因结果与双视角账单）。
"""

from __future__ import annotations

from app.config import get_config
from app.db import SessionLocal, init_db
from app.services.seed import seed_orders


def main() -> None:
    cfg = get_config()
    init_db()
    with SessionLocal() as session:
        orders = seed_orders(session)
        session.commit()
    print(f"[seed] config_version={cfg.config_version} algorithm_version={cfg.algorithm_version}")
    for order in orders:
        attribution = order.attribution
        print(
            f"[seed] {order.order_no:<8} quoted={order.estimate.quoted_price:>6} "
            f"paid={order.bill.paid_amount:>6} delta={attribution.total_delta:>6} "
            f"residual={attribution.residual:>5} quality={attribution.data_quality}"
        )


if __name__ == "__main__":
    main()
