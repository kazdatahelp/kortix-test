#!/usr/bin/env python3.11
"""SKU Dashboard API — FastAPI backend for SKK price monitoring.
Serves product list, region list, and computed dashboard data
from eoz_full_auth.db. IBCS-compliant.
"""
import sqlite3, statistics, os
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

DB_PATH = Path(os.environ.get("EOZ_DB", Path(__file__).parent / "eoz_full_auth.db"))
OUTPUT_DIR = Path(__file__).parent / "output"

app = FastAPI(title="SKK Dashboard API", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

TRU_TYPE_MAP = {1: "товар", 2: "работа", 3: "услуга"}

# ── DB helpers ────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    return db

# ── API Endpoints ─────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"status": "ok", "db": str(DB_PATH), "exists": DB_PATH.exists()}

@app.get("/api/products")
def list_products(
    q: Optional[str] = Query(None, description="Search by name or code"),
    tru_type: Optional[int] = Query(None, description="1=товар, 2=работа, 3=услуга"),
):
    """List all available ENSTRU codes with contract counts."""
    db = get_db()
    try:
        where = ["c.status = 'EXECUTED'"]
        params = []
        if q:
            where.append("(pe.enstru LIKE ? OR pe.name LIKE ?)")
            params.extend([f"%{q}%", f"%{q}%"])
        if tru_type is not None:
            where.append("de.tru_type = ?")
            params.append(tru_type)

        sql = f"""
            SELECT pe.enstru, pe.name, de.tru_type,
                   COUNT(*) as contract_count,
                   COUNT(DISTINCT c.provider_bin) as supplier_count,
                   ROUND(AVG(c.amt / NULLIF(c.qty, 0)), 2) as avg_unit_price,
                   SUM(c.amt) as total_sum,
                   MIN(c.year) as year_from, MAX(c.year) as year_to
            FROM skk_contract c
            JOIN skk_plan_enstru pe ON c.plan_id = pe.plan_id
            LEFT JOIN skk_dim_enstru de ON pe.enstru = de.code
            WHERE {' AND '.join(where)}
            GROUP BY pe.enstru
            ORDER BY contract_count DESC
            LIMIT 100
        """
        rows = db.execute(sql, params).fetchall()
        return [{
            "enstru": r["enstru"],
            "name": r["name"] or "",
            "tru_type": TRU_TYPE_MAP.get(r["tru_type"], "неизвестно"),
            "contract_count": r["contract_count"],
            "supplier_count": r["supplier_count"],
            "avg_unit_price": r["avg_unit_price"],
            "total_sum": r["total_sum"],
            "year_from": r["year_from"],
            "year_to": r["year_to"],
        } for r in rows]
    finally:
        db.close()

@app.get("/api/regions")
def list_regions(enstru: Optional[str] = Query(None)):
    """List regions with contract counts for a given ENSTRU (or all)."""
    db = get_db()
    try:
        joins = []
        where = ["c.status = 'EXECUTED'"]
        params = []
        if enstru:
            joins.append("JOIN skk_plan_enstru pe ON c.plan_id = pe.plan_id")
            where.append("pe.enstru = ?")
            params.append(enstru)

        sql = f"""
            SELECT rk.ab as region_code, rk.full_name_ru as region_name,
                   COUNT(*) as contract_count, COUNT(DISTINCT c.provider_bin) as supplier_count
            FROM skk_contract c
            {' '.join(joins)}
            JOIN skk_ref_kato rk ON CAST(c.delivery_kato_code / 10000000 AS INTEGER) = CAST(rk.ab AS INTEGER)
            WHERE {' AND '.join(where)}
            GROUP BY rk.ab
            ORDER BY contract_count DESC
        """
        rows = db.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()

@app.get("/api/dashboard")
def get_dashboard(
    enstru: str = Query("192021.530.000001", description="ENSTRU code"),
    region: Optional[str] = Query(None, description="Region code (kato AB prefix)"),
    year_from: int = Query(2024, ge=2020, le=2030),
    year_to: int = Query(2026, ge=2020, le=2030),
    exclude_rnu: bool = Query(True),
    status: str = Query("EXECUTED", description="Contract status filter"),
):
    """Compute full dashboard data for selected filters."""
    db = get_db()
    try:
        # Base query: contracts filtered by ENSTRU + year + status
        joins = ["JOIN skk_plan_enstru pe ON c.plan_id = pe.plan_id"]
        where = ["pe.enstru = ?", "c.year BETWEEN ? AND ?", "c.status = ?"]
        params = [enstru, year_from, year_to, status]

        if region:
            where.append("(CAST(c.delivery_kato_code / 10000000 AS INTEGER) = CAST(? AS INTEGER))")
            params.append(region)

        # Get product name
        prod = db.execute(
            "SELECT pe.name, de.tru_type FROM skk_plan_enstru pe LEFT JOIN skk_dim_enstru de ON pe.enstru = de.code WHERE pe.enstru = ? LIMIT 1",
            [enstru]
        ).fetchone()
        product_name = prod["name"] if prod else enstru
        tru_type = TRU_TYPE_MAP.get(prod["tru_type"], "неизвестно") if prod and prod["tru_type"] else "неизвестно"

        # Get all matching contracts with unit prices
        sql = f"""
            SELECT c.contract_card_id, c.provider_bin, c.plan_id, c.amt, c.qty,
                   c.delivery_kato_code, c.year, c.status,
                   ROUND(c.amt / NULLIF(c.qty, 0), 2) as unit_price
            FROM skk_contract c
            {' '.join(joins)}
            WHERE {' AND '.join(where)}
              AND c.qty > 0 AND c.amt > 0
        """
        contracts = db.execute(sql, params).fetchall()

        if not contracts:
            return JSONResponse({"error": "No contracts found", "count": 0}, status_code=404)

        # Unit prices for statistics
        prices = [r["unit_price"] for r in contracts if r["unit_price"] and r["unit_price"] > 0]
        n = len(prices)

        if n < 3:
            return JSONResponse({"error": "Insufficient data (<3 valid prices)", "count": n}, status_code=400)

        prices_sorted = sorted(prices)
        p25 = prices_sorted[int(n * 0.25)]
        p50 = statistics.median(prices_sorted)
        p75 = prices_sorted[int(n * 0.75)]
        spread = ((p75 - p25) / p50 * 100) if p50 > 0 else 0
        corridor = 0.20
        corridor_low = p50 * (1 - corridor)
        corridor_high = p50 * (1 + corridor)

        # Confidence class (IBCS)
        unique_suppliers = len(set(r["provider_bin"] for r in contracts))
        if n >= 30 and spread < 150:
            conf_class = "A (надёжно)"
        elif n >= 8:
            conf_class = "B (ограниченно)"
        else:
            conf_class = "C (ненадёжно)"

        # Price reference
        price_reference = {
            "enstru": enstru,
            "name": product_name,
            "tru_type": tru_type,
            "n_items": n,
            "n_suppliers": unique_suppliers,
            "n_contracts": len(contracts),
            "median_price": round(p50, 2),
            "mode_price": round(max(set(prices), key=prices.count), 2) if prices else None,
            "p25": round(p25, 2),
            "p75": round(p75, 2),
            "spread_pct": round(spread, 3),
            "conf_class": conf_class,
            "corridor_low": round(corridor_low, 2),
            "corridor_high": round(corridor_high, 2),
            "national_share": 1.0 if not region else round(n / n, 3),
            "regional_share": round(sum(1 for p in prices if corridor_low <= p <= corridor_high) / n, 3) if n > 0 else 0,
            "price_basis": "региональный" if region else "национальный",
            "needs_segmentation": spread > 150
        }

        # Regional breakdown
        region_rows = db.execute(f"""
            SELECT CAST(c.delivery_kato_code / 10000000 AS INTEGER) as region_code,
                   rk.full_name_ru as region_name,
                   COUNT(*) as n_items,
                   ROUND(AVG(c.amt / NULLIF(c.qty, 0)), 2) as median_price,
                   COUNT(*) as cnt
            FROM skk_contract c
            {' '.join(joins)}
            JOIN skk_ref_kato rk ON CAST(c.delivery_kato_code / 10000000 AS INTEGER) = CAST(rk.ab AS INTEGER)
            WHERE {' AND '.join(where)}
              AND c.qty > 0 AND c.amt > 0
            GROUP BY region_code
            ORDER BY n_items DESC
        """, params).fetchall()

        price_by_region = []
        for r in region_rows:
            # Get P25/P75 per region
            rp = db.execute(f"""
                SELECT ROUND(c.amt / NULLIF(c.qty, 0), 2) as up
                FROM skk_contract c
                {' '.join(joins)}
                WHERE {' AND '.join(where)}
                  AND c.qty > 0 AND c.amt > 0
                  AND CAST(c.delivery_kato_code / 10000000 AS INTEGER) = ?
                ORDER BY up
            """, params + [r["region_code"]]).fetchall()
            rprices = [x["up"] for x in rp if x["up"] and x["up"] > 0]
            if len(rprices) >= 3:
                rp_s = sorted(rprices)
                rp25 = rp_s[int(len(rp_s) * 0.25)]
                rp75 = rp_s[int(len(rp_s) * 0.75)]
                in_corridor = sum(1 for p in rprices if corridor_low <= p <= corridor_high) / len(rprices)
            else:
                rp25, rp75, in_corridor = r["median_price"], r["median_price"], 1.0

            price_by_region.append({
                "enstru": enstru,
                "name": product_name,
                "region": r["region_name"] or f"Код {r['region_code']}",
                "n_items": r["n_items"],
                "median_price": r["median_price"],
                "p25": round(rp25, 2),
                "p75": round(rp75, 2),
                "corridor_low": round(corridor_low, 2),
                "corridor_high": round(corridor_high, 2),
                "in_corridor_share": round(in_corridor, 3),
            })

        # Supplier registry
        supplier_sql = f"""
            SELECT c.provider_bin,
                   COALESCE(ds.name_ru, s.name_ru, c.provider_bin) as supplier_name,
                   COALESCE(ds.is_rnu, 0) as is_rnu,
                   COALESCE(ds.kato, s.jur_kato_ru, '') as legal_kato,
                   COUNT(*) as n_contracts,
                   COUNT(DISTINCT c.contract_card_id) as n_items,
                   SUM(CASE WHEN c.status = 'EXECUTED' THEN 1 ELSE 0 END) as n_exec_items,
                   SUM(c.amt) as total_sum
            FROM skk_contract c
            {' '.join(joins)}
            LEFT JOIN skk_dim_subject ds ON c.provider_bin = ds.bin_iin
            LEFT JOIN skk_subject s ON c.provider_bin = s.bin_iin
            WHERE {' AND '.join(where)}
            GROUP BY c.provider_bin
            ORDER BY total_sum DESC
        """
        supplier_rows = db.execute(supplier_sql, params).fetchall()

        # Get delivery regions for each supplier
        supplier_registry = []
        for sr in supplier_rows:
            if exclude_rnu and sr["is_rnu"]:
                continue

            # Delivery regions
            dr = db.execute(f"""
                SELECT DISTINCT rk.full_name_ru
                FROM skk_contract c
                {' '.join(joins)}
                JOIN skk_ref_kato rk ON CAST(c.delivery_kato_code / 10000000 AS INTEGER) = CAST(rk.ab AS INTEGER)
                WHERE {' AND '.join(where)}
                  AND c.provider_bin = ?
                ORDER BY rk.full_name_ru
                LIMIT 10
            """, params + [sr["provider_bin"]]).fetchall()

            # Years active
            ya = db.execute(f"""
                SELECT MIN(c.year) as ymin, MAX(c.year) as ymax
                FROM skk_contract c
                {' '.join(joins)}
                WHERE {' AND '.join(where)}
                  AND c.provider_bin = ?
            """, params + [sr["provider_bin"]]).fetchone()

            years_str = f"{ya['ymin']}" if ya["ymin"] == ya["ymax"] else f"{ya['ymin']}–{ya['ymax']}"

            supplier_registry.append({
                "supplier_bin": sr["provider_bin"],
                "supplier_name": sr["supplier_name"] or sr["provider_bin"],
                "is_rnu": bool(sr["is_rnu"]),
                "legal_region": sr["legal_kato"] or "",
                "delivery_regions": "; ".join(d["full_name_ru"] for d in dr if d["full_name_ru"]),
                "n_contracts": sr["n_contracts"],
                "n_items": sr["n_items"],
                "n_exec_items": sr["n_exec_items"],
                "total_sum": sr["total_sum"] or 0,
                "years_active": years_str,
            })

        # Year distribution
        year_dist = db.execute(f"""
            SELECT c.year, COUNT(*) as cnt
            FROM skk_contract c
            {' '.join(joins)}
            WHERE {' AND '.join(where)}
            GROUP BY c.year
            ORDER BY c.year
        """, params).fetchall()

        # Recent contracts (top 30)
        recent = db.execute(f"""
            SELECT c.contract_card_id as id,
                   COALESCE(ds.name_ru, s.name_ru, c.provider_bin) as provider,
                   c.year, c.qty, c.amt,
                   ROUND(c.amt / NULLIF(c.qty, 0), 2) as unit_price,
                   rk.full_name_ru as region,
                   c.status
            FROM skk_contract c
            {' '.join(joins)}
            LEFT JOIN skk_dim_subject ds ON c.provider_bin = ds.bin_iin
            LEFT JOIN skk_subject s ON c.provider_bin = s.bin_iin
            LEFT JOIN skk_ref_kato rk ON CAST(c.delivery_kato_code / 10000000 AS INTEGER) = CAST(rk.ab AS INTEGER)
            WHERE {' AND '.join(where)}
            ORDER BY c.contract_card_id DESC
            LIMIT 30
        """, params).fetchall()

        return {
            "filters": {"enstru": enstru, "region": region, "year_from": year_from, "year_to": year_to, "exclude_rnu": exclude_rnu},
            "product_name": product_name,
            "tru_type": tru_type,
            "total_volume": sum(r["amt"] for r in contracts),
            "total_quantity": sum(r["qty"] for r in contracts),
            "price_reference": [price_reference],
            "price_by_region": price_by_region,
            "supplier_registry": supplier_registry,
            "year_distribution": [{"year": r["year"], "count": r["cnt"]} for r in year_dist],
            "contracts": [{
                "id": str(r["id"]), "provider": r["provider"] or r["id"], "year": r["year"],
                "region": r["region"] or "—", "qty": r["qty"], "amt": r["amt"],
                "unit_price": r["unit_price"], "status": r["status"]
            } for r in recent],
        }
    finally:
        db.close()

@app.get("/api/report")
def generate_report(enstru: str = Query("192021.530.000001")):
    """Trigger SKK pipeline report generation (Excel + DOCX)."""
    import subprocess, sys
    cmd = [
        sys.executable, "-c",
        f"from skk.intake import run_intake; run_intake(['{enstru}']); "
        "from skk.filter import run_filter; run_filter(); "
        "from skk.prices import run_prices; run_prices(); "
        "from skk.pool import run_pool; run_pool(); "
        "from skk.report import render; print(render())"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(Path(__file__).parent), timeout=120)
    return {
        "success": result.returncode == 0,
        "output": result.stdout[-500:] if result.stdout else "",
        "error": result.stderr[-500:] if result.stderr else "",
        "files": [str(p.relative_to(OUTPUT_DIR)) for p in OUTPUT_DIR.glob("Мониторинг_*") if p.is_file()]
    }

# ── Static files ──────────────────────────────────────────

@app.get("/")
def serve_dashboard():
    dashboard_path = Path(__file__).parent / "skk-dashboard.html"
    if dashboard_path.exists():
        return FileResponse(dashboard_path, media_type="text/html")
    return {"message": "SKK Dashboard API running. Open /skk-dashboard.html"}
