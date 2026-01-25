from datetime import datetime, timedelta, timezone
import os
from typing import Dict, Iterable, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, distinct
from sqlalchemy.orm import Session

from auth_routes import get_current_user
from database import get_db
from models import Essay, MercadoPagoPayment, User

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None


router = APIRouter(prefix="/admin", tags=["admin"])

ADMIN_EMAILS = {
    email.strip().lower()
    for email in (os.environ.get("ADMIN_EMAILS", "")).split(",")
    if email.strip()
}

PRICE_PER_CREDIT = float(os.environ.get("PRICE_PER_CREDIT", "0.99"))

GROUP_BY_VALUES = {"day", "week", "month"}
MAX_BUCKETS = {"day": 366, "week": 260, "month": 120}


def _get_tz(timezone_name: str):
    if not ZoneInfo:
        raise HTTPException(status_code=500, detail="ZoneInfo indisponível.")
    try:
        return ZoneInfo(timezone_name)
    except Exception:
        raise HTTPException(status_code=400, detail="Timezone inválida.")


def _parse_iso(dt_str: str, tz) -> datetime:
    try:
        if dt_str.endswith("Z"):
            dt_str = dt_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(dt_str)
    except Exception:
        raise HTTPException(status_code=400, detail="Datetime inválido.")
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def _parse_period(
    start: Optional[str],
    end: Optional[str],
    timezone_name: str,
    default_days: Optional[int],
) -> Tuple[Optional[datetime], Optional[datetime], Optional[object]]:
    tz = _get_tz(timezone_name)
    if not start and not end:
        if default_days is None:
            return None, None, tz
        end_dt = datetime.now(tz)
        start_dt = end_dt - timedelta(days=default_days)
        return start_dt, end_dt, tz
    if not start or not end:
        raise HTTPException(
            status_code=400, detail="Envie start e end juntos."
        )
    start_dt = _parse_iso(start, tz)
    end_dt = _parse_iso(end, tz)
    if end_dt <= start_dt:
        raise HTTPException(status_code=400, detail="end deve ser maior que start.")
    return start_dt, end_dt, tz


def _ensure_group_by(group_by: str) -> str:
    group_by = (group_by or "day").lower()
    if group_by not in GROUP_BY_VALUES:
        raise HTTPException(status_code=400, detail="group_by inválido.")
    return group_by


def _bucket_start(dt: datetime, group_by: str) -> datetime:
    if group_by == "day":
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if group_by == "week":
        monday = dt - timedelta(days=dt.weekday())
        return monday.replace(hour=0, minute=0, second=0, microsecond=0)
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _iter_buckets(start: datetime, end: datetime, group_by: str) -> List[datetime]:
    buckets = []
    current = _bucket_start(start, group_by)
    if current < start:
        current = _advance_bucket(current, group_by)
    while current < end:
        buckets.append(current)
        current = _advance_bucket(current, group_by)
    return buckets


def _advance_bucket(dt: datetime, group_by: str) -> datetime:
    if group_by == "day":
        return dt + timedelta(days=1)
    if group_by == "week":
        return dt + timedelta(days=7)
    year = dt.year + (dt.month // 12)
    month = (dt.month % 12) + 1
    return dt.replace(year=year, month=month, day=1)


def _clamp_range(start: datetime, end: datetime, group_by: str) -> Tuple[datetime, datetime]:
    max_buckets = MAX_BUCKETS[group_by]
    buckets = _iter_buckets(start, end, group_by)
    if len(buckets) <= max_buckets:
        return start, end
    # recorta para manter os últimos buckets
    return buckets[-max_buckets], end


def _to_utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc)


def _require_admin(current_user: User = Depends(get_current_user)) -> User:
    is_admin = getattr(current_user, "is_admin", False)
    if is_admin:
        return current_user
    if current_user.email and current_user.email.lower() in ADMIN_EMAILS:
        return current_user
    raise HTTPException(status_code=403, detail="Acesso restrito.")


def _dialect_name(db: Session) -> str:
    return db.bind.dialect.name if db.bind else ""


def _series_from_results(
    *,
    buckets: List[datetime],
    results: Iterable[Tuple[datetime, int]],
) -> Dict[str, List]:
    counts = {bucket: 0 for bucket in buckets}
    for bucket, count in results:
        if bucket in counts:
            counts[bucket] = count
    labels = [bucket.isoformat() for bucket in buckets]
    series = [counts[bucket] for bucket in buckets]
    return {"labels": labels, "series": series}


def _query_series_count(
    *,
    db: Session,
    model,
    date_field,
    start_utc: datetime,
    end_utc: datetime,
    tz_name: str,
    group_by: str,
    extra_filters: Optional[List] = None,
) -> List[Tuple[datetime, int]]:
    extra_filters = extra_filters or []
    dialect = _dialect_name(db)
    tz = _get_tz(tz_name)
    if dialect == "postgresql":
        bucket_expr = func.date_trunc(
            group_by, func.timezone(tz_name, date_field)
        )
        query = (
            db.query(bucket_expr.label("bucket"), func.count())
            .filter(date_field >= start_utc, date_field < end_utc, *extra_filters)
            .group_by(bucket_expr)
            .order_by(bucket_expr)
        )
        results = []
        for row in query.all():
            bucket = row.bucket
            if bucket and bucket.tzinfo is None:
                bucket = bucket.replace(tzinfo=tz)
            results.append((bucket, int(row[1])))
        return results

    # Fallback: agrupa em Python (SQLite)
    rows = (
        db.query(date_field)
        .filter(date_field >= start_utc, date_field < end_utc, *extra_filters)
        .all()
    )
    buckets: Dict[datetime, int] = {}
    for (dt,) in rows:
        if dt is None:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local_dt = dt.astimezone(tz)
        bucket = _bucket_start(local_dt, group_by)
        buckets[bucket] = buckets.get(bucket, 0) + 1
    return sorted(buckets.items(), key=lambda item: item[0])


@router.get("/metrics/overview")
def metrics_overview(
    start: Optional[str] = Query(default=None),
    end: Optional[str] = Query(default=None),
    timezone: str = Query(default="America/Sao_Paulo"),
    db: Session = Depends(get_db),
    current_user: User = Depends(_require_admin),
):
    start_local, end_local, tz = _parse_period(start, end, timezone, default_days=None)
    if start_local and end_local:
        start_utc = _to_utc(start_local)
        end_utc = _to_utc(end_local)
        users_created = (
            db.query(func.count(User.id))
            .filter(User.created_at >= start_utc, User.created_at < end_utc)
            .scalar()
        )
        corrections = (
            db.query(func.count(Essay.id))
            .filter(
                Essay.created_at >= start_utc,
                Essay.created_at < end_utc,
                Essay.nota_final.isnot(None),
            )
            .scalar()
        )
        sales_approved = (
            db.query(func.count(MercadoPagoPayment.id))
            .filter(
                MercadoPagoPayment.created_at >= start_utc,
                MercadoPagoPayment.created_at < end_utc,
                MercadoPagoPayment.status == "approved",
            )
            .scalar()
        )
        sales_credited = (
            db.query(func.count(MercadoPagoPayment.id))
            .filter(
                MercadoPagoPayment.created_at >= start_utc,
                MercadoPagoPayment.created_at < end_utc,
                MercadoPagoPayment.credited.is_(True),
            )
            .scalar()
        )
        credits_sold_credited = (
            db.query(func.coalesce(func.sum(MercadoPagoPayment.credits), 0))
            .filter(
                MercadoPagoPayment.created_at >= start_utc,
                MercadoPagoPayment.created_at < end_utc,
                MercadoPagoPayment.credited.is_(True),
            )
            .scalar()
        )
        active_users = (
            db.query(func.count(distinct(Essay.user_id)))
            .filter(
                Essay.created_at >= start_utc,
                Essay.created_at < end_utc,
                Essay.nota_final.isnot(None),
            )
            .scalar()
        )
    else:
        users_created = db.query(func.count(User.id)).scalar()
        corrections = (
            db.query(func.count(Essay.id))
            .filter(Essay.nota_final.isnot(None))
            .scalar()
        )
        sales_approved = (
            db.query(func.count(MercadoPagoPayment.id))
            .filter(MercadoPagoPayment.status == "approved")
            .scalar()
        )
        sales_credited = (
            db.query(func.count(MercadoPagoPayment.id))
            .filter(MercadoPagoPayment.credited.is_(True))
            .scalar()
        )
        credits_sold_credited = (
            db.query(func.coalesce(func.sum(MercadoPagoPayment.credits), 0))
            .filter(MercadoPagoPayment.credited.is_(True))
            .scalar()
        )
        active_users = (
            db.query(func.count(distinct(Essay.user_id)))
            .filter(Essay.nota_final.isnot(None))
            .scalar()
        )

    estimated_revenue = float(credits_sold_credited or 0) * PRICE_PER_CREDIT

    return {
        "users_created": int(users_created or 0),
        "corrections": int(corrections or 0),
        "sales_approved": int(sales_approved or 0),
        "sales_credited": int(sales_credited or 0),
        "credits_sold_credited": int(credits_sold_credited or 0),
        "estimated_revenue": round(estimated_revenue, 2),
        "active_users": int(active_users or 0),
    }


@router.get("/metrics/users/created")
def users_created_series(
    start: Optional[str] = Query(default=None),
    end: Optional[str] = Query(default=None),
    timezone: str = Query(default="America/Sao_Paulo"),
    group_by: str = Query(default="day"),
    db: Session = Depends(get_db),
    current_user: User = Depends(_require_admin),
):
    group_by = _ensure_group_by(group_by)
    start_local, end_local, tz = _parse_period(start, end, timezone, default_days=30)
    start_local, end_local = _clamp_range(start_local, end_local, group_by)
    start_utc = _to_utc(start_local)
    end_utc = _to_utc(end_local)

    results = _query_series_count(
        db=db,
        model=User,
        date_field=User.created_at,
        start_utc=start_utc,
        end_utc=end_utc,
        tz_name=timezone,
        group_by=group_by,
    )
    buckets = _iter_buckets(start_local, end_local, group_by)
    series = _series_from_results(buckets=buckets, results=results)

    total = (
        db.query(func.count(User.id))
        .filter(User.created_at >= start_utc, User.created_at < end_utc)
        .scalar()
    )
    return {"total": int(total or 0), **series}


@router.get("/metrics/corrections")
def corrections_series(
    start: Optional[str] = Query(default=None),
    end: Optional[str] = Query(default=None),
    timezone: str = Query(default="America/Sao_Paulo"),
    group_by: str = Query(default="day"),
    db: Session = Depends(get_db),
    current_user: User = Depends(_require_admin),
):
    group_by = _ensure_group_by(group_by)
    start_local, end_local, tz = _parse_period(start, end, timezone, default_days=30)
    start_local, end_local = _clamp_range(start_local, end_local, group_by)
    start_utc = _to_utc(start_local)
    end_utc = _to_utc(end_local)

    results = _query_series_count(
        db=db,
        model=Essay,
        date_field=Essay.created_at,
        start_utc=start_utc,
        end_utc=end_utc,
        tz_name=timezone,
        group_by=group_by,
        extra_filters=[Essay.nota_final.isnot(None)],
    )
    buckets = _iter_buckets(start_local, end_local, group_by)
    series = _series_from_results(buckets=buckets, results=results)

    total = (
        db.query(func.count(Essay.id))
        .filter(
            Essay.created_at >= start_utc,
            Essay.created_at < end_utc,
            Essay.nota_final.isnot(None),
        )
        .scalar()
    )
    return {"total": int(total or 0), **series}


@router.get("/metrics/sales")
def sales_series(
    start: Optional[str] = Query(default=None),
    end: Optional[str] = Query(default=None),
    timezone: str = Query(default="America/Sao_Paulo"),
    group_by: str = Query(default="day"),
    db: Session = Depends(get_db),
    current_user: User = Depends(_require_admin),
):
    group_by = _ensure_group_by(group_by)
    start_local, end_local, tz = _parse_period(start, end, timezone, default_days=30)
    start_local, end_local = _clamp_range(start_local, end_local, group_by)
    start_utc = _to_utc(start_local)
    end_utc = _to_utc(end_local)

    approved_results = _query_series_count(
        db=db,
        model=MercadoPagoPayment,
        date_field=MercadoPagoPayment.created_at,
        start_utc=start_utc,
        end_utc=end_utc,
        tz_name=timezone,
        group_by=group_by,
        extra_filters=[MercadoPagoPayment.status == "approved"],
    )
    credited_results = _query_series_count(
        db=db,
        model=MercadoPagoPayment,
        date_field=MercadoPagoPayment.created_at,
        start_utc=start_utc,
        end_utc=end_utc,
        tz_name=timezone,
        group_by=group_by,
        extra_filters=[MercadoPagoPayment.credited.is_(True)],
    )

    buckets = _iter_buckets(start_local, end_local, group_by)
    approved_series = _series_from_results(
        buckets=buckets, results=approved_results
    )
    credited_series = _series_from_results(
        buckets=buckets, results=credited_results
    )

    total_approved_all = (
        db.query(func.count(MercadoPagoPayment.id))
        .filter(MercadoPagoPayment.status == "approved")
        .scalar()
    )
    total_credited_all = (
        db.query(func.count(MercadoPagoPayment.id))
        .filter(MercadoPagoPayment.credited.is_(True))
        .scalar()
    )

    total_approved_period = (
        db.query(func.count(MercadoPagoPayment.id))
        .filter(
            MercadoPagoPayment.created_at >= start_utc,
            MercadoPagoPayment.created_at < end_utc,
            MercadoPagoPayment.status == "approved",
        )
        .scalar()
    )
    total_credited_period = (
        db.query(func.count(MercadoPagoPayment.id))
        .filter(
            MercadoPagoPayment.created_at >= start_utc,
            MercadoPagoPayment.created_at < end_utc,
            MercadoPagoPayment.credited.is_(True),
        )
        .scalar()
    )

    return {
        "totals": {
            "approved_all": int(total_approved_all or 0),
            "credited_all": int(total_credited_all or 0),
            "approved_period": int(total_approved_period or 0),
            "credited_period": int(total_credited_period or 0),
        },
        "labels": approved_series["labels"],
        "series": {
            "approved": approved_series["series"],
            "credited": credited_series["series"],
        },
    }


@router.get("/metrics/corrections/by-user")
def corrections_by_user(
    start: Optional[str] = Query(default=None),
    end: Optional[str] = Query(default=None),
    timezone: str = Query(default="America/Sao_Paulo"),
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(_require_admin),
):
    start_local, end_local, tz = _parse_period(start, end, timezone, default_days=None)
    filters = [Essay.nota_final.isnot(None)]
    if start_local and end_local:
        start_utc = _to_utc(start_local)
        end_utc = _to_utc(end_local)
        filters.extend([Essay.created_at >= start_utc, Essay.created_at < end_utc])

    total_corrections = (
        db.query(func.count(Essay.id))
        .filter(*filters)
        .scalar()
    ) or 0

    rows = (
        db.query(
            User.id,
            User.email,
            User.full_name,
            func.count(Essay.id).label("corrigidas"),
        )
        .join(Essay, Essay.user_id == User.id)
        .filter(*filters)
        .group_by(User.id, User.email, User.full_name)
        .order_by(func.count(Essay.id).desc())
        .limit(limit)
        .all()
    )

    results = []
    for row in rows:
        percent = (
            (row.corrigidas / total_corrections) * 100
            if total_corrections
            else 0
        )
        results.append(
            {
                "user_id": row.id,
                "email": row.email,
                "full_name": row.full_name,
                "corrigidas": int(row.corrigidas),
                "percent": round(percent, 2),
            }
        )

    return {
        "total_corrections": int(total_corrections),
        "results": results,
    }
