from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Tuple

import pandas as pd
import pdfplumber
import streamlit as st


# -----------------------------
# Parsing & Data preparation
# -----------------------------

NUMBER_REGEX = re.compile(r"-?\d+[\.,]?\d*")


def _to_float(value: str | float | int | None) -> float:
    """Convert strings with locale-specific decimal separators to float."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)

    raw = str(value).strip()
    if not raw:
        return 0.0

    match = NUMBER_REGEX.search(raw.replace(" ", ""))
    if not match:
        return 0.0

    normalized = match.group(0).replace(".", "").replace(",", ".")
    try:
        return float(normalized)
    except ValueError:
        return 0.0


def _to_hours(value: str | None) -> float:
    """Convert HH:MM-like duration strings to decimal hours."""
    if not value:
        return 0.0
    v = str(value).strip()
    if ":" in v:
        parts = v.split(":")
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            return int(parts[0]) + int(parts[1]) / 60
    return _to_float(v)


def _parse_date(value: str | None) -> pd.Timestamp | pd.NaT:
    if not value:
        return pd.NaT
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d"):
        try:
            return pd.Timestamp(datetime.strptime(value.strip(), fmt).date())
        except ValueError:
            continue
    return pd.to_datetime(value, errors="coerce")


def _extract_text_tables(pdf_bytes: bytes) -> List[List[str]]:
    rows: List[List[str]] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables() or []
            for table in tables:
                for row in table:
                    if row and any(cell for cell in row):
                        rows.append([str(cell).strip() if cell else "" for cell in row])
    return rows


def parse_webfleet_pdf(pdf_bytes: bytes) -> pd.DataFrame:
    """
    Parse Webfleet weekly tour reports and return cleaned rows per vehicle/date.

    Expected columns in the PDF table (names may vary):
    - Kennzeichen/Fahrzeug
    - Datum
    - Entfernung
    - Kraftstoffverbrauch
    - Stillstand
    - Startposition
    - Endposition
    """
    rows = _extract_text_tables(pdf_bytes)

    # flatten rows from likely report layout
    parsed: List[Dict[str, object]] = []
    for r in rows:
        joined = " | ".join(r)
        if "WIT-HB" not in joined:
            continue

        plate = next((c for c in r if "WIT-HB" in c), "")

        # heuristics for date / metrics
        date = next((c for c in r if re.search(r"\d{1,2}\.\d{1,2}\.\d{2,4}", c or "")), "")
        km = next((c for c in r if "km" in (c or "").lower()), "")
        liters = next((c for c in r if "l" in (c or "").lower() and "km" not in (c or "").lower()), "")
        standstill = next(
            (c for c in r if "h" in (c or "").lower() or re.match(r"^\d{1,2}:\d{2}$", c or "")),
            "",
        )

        start_pos = r[-2] if len(r) >= 2 else ""
        end_pos = r[-1] if len(r) >= 1 else ""

        parsed.append(
            {
                "datum": _parse_date(date),
                "lkw_id": plate.strip(),
                "gps_km_real": _to_float(km),
                "kraftstoff_l": _to_float(liters),
                "stillstand_h": _to_hours(standstill),
                "start_position": start_pos,
                "end_position": end_pos,
            }
        )

    df = pd.DataFrame(parsed)
    if df.empty:
        return pd.DataFrame(
            columns=[
                "datum",
                "lkw_id",
                "gps_km_real",
                "kraftstoff_l",
                "stillstand_h",
                "start_position",
                "end_position",
            ]
        )

    df = df[df["lkw_id"].str.startswith("WIT-HB", na=False)].copy()
    df["datum"] = pd.to_datetime(df["datum"], errors="coerce").dt.date
    for c in ["gps_km_real", "kraftstoff_l", "stillstand_h"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)

    df = (
        df.groupby(["datum", "lkw_id"], as_index=False)
        .agg(
            {
                "gps_km_real": "sum",
                "kraftstoff_l": "sum",
                "stillstand_h": "sum",
                "start_position": "first",
                "end_position": "last",
            }
        )
        .sort_values(["datum", "lkw_id"])
    )
    return df


# -----------------------------
# Controlling Logic
# -----------------------------


@dataclass
class CostSetup:
    fixkosten_pro_tag: float
    variabler_km_satz: float
    dieselpreis_pro_l: float


def compute_costing(real_df: pd.DataFrame, planning_df: pd.DataFrame, setup: CostSetup) -> pd.DataFrame:
    merged = planning_df.merge(
        real_df,
        on=["datum", "lkw_id"],
        how="left",
        suffixes=("_plan", "_real"),
    )

    for c in ["gps_km_real", "kraftstoff_l", "stillstand_h"]:
        merged[c] = pd.to_numeric(merged.get(c, 0), errors="coerce").fillna(0.0)

    merged["fixkosten"] = setup.fixkosten_pro_tag
    merged["variable_kosten"] = merged["gps_km_real"] * setup.variabler_km_satz
    merged["kraftstoffkosten"] = merged["kraftstoff_l"] * setup.dieselpreis_pro_l
    merged["gesamtkosten"] = merged[["fixkosten", "variable_kosten", "kraftstoffkosten"]].sum(axis=1)
    merged["deckungsbeitrag"] = merged["erloes_plan"] - merged["gesamtkosten"]
    merged["delta_km"] = merged["gps_km_plan"] - merged["gps_km_real"]
    return merged


# -----------------------------
# Depot Logic
# -----------------------------


def compute_inventory(journal_df: pd.DataFrame, startbestand_1: float, startbestand_2: float) -> Tuple[pd.DataFrame, pd.DataFrame]:
    j = journal_df.copy()
    j["menge_t"] = pd.to_numeric(j["menge_t"], errors="coerce").fillna(0.0)
    j["vorzeichen"] = j["art"].str.lower().map({"eingang": 1, "ausgang": -1}).fillna(0)
    j["signed_menge"] = j["menge_t"] * j["vorzeichen"]

    base = {"Lager 1": startbestand_1, "Lager 2": startbestand_2}
    stock = j.groupby("depot", as_index=False)["signed_menge"].sum()
    stock["startbestand_t"] = stock["depot"].map(base).fillna(0.0)
    stock["bestand_t"] = stock["startbestand_t"] + stock["signed_menge"]

    for depot in ["Lager 1", "Lager 2"]:
        if depot not in stock["depot"].values:
            stock = pd.concat(
                [
                    stock,
                    pd.DataFrame(
                        [{"depot": depot, "signed_menge": 0.0, "startbestand_t": base[depot], "bestand_t": base[depot]}]
                    ),
                ],
                ignore_index=True,
            )

    stock = stock.sort_values("depot")
    return j, stock


# -----------------------------
# UI
# -----------------------------


def init_state() -> None:
    if "planning_df" not in st.session_state:
        st.session_state.planning_df = pd.DataFrame(
            columns=["datum", "prio", "lkw_id", "projekt", "material", "depot", "gps_km_plan", "erloes_plan"]
        )
    if "journal_df" not in st.session_state:
        st.session_state.journal_df = pd.DataFrame(columns=["datum", "depot", "material", "art", "menge_t"])


def planning_form() -> None:
    st.subheader("Planungs-Maske")
    with st.form("planung_form", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        datum = c1.date_input("Datum")
        prio = c2.selectbox("Prio", ["Hoch", "Mittel", "Niedrig"])
        lkw_id = c3.text_input("LKW-ID", value="WIT-HB-")

        c4, c5, c6 = st.columns(3)
        projekt = c4.text_input("Projekt")
        material = c5.text_input("Material")
        depot = c6.selectbox("Depot", ["Lager 1", "Lager 2"])

        c7, c8 = st.columns(2)
        gps_km_plan = c7.number_input("Geplante KM", min_value=0.0, step=1.0)
        erloes_plan = c8.number_input("Erlös (Plan)", min_value=0.0, step=50.0)

        submitted = st.form_submit_button("Planung speichern")

        if submitted:
            if "spiekermann" in projekt.lower() and erloes_plan == 0:
                erloes_plan = 650.0  # Sonderpreis-Default
            new_row = pd.DataFrame(
                [
                    {
                        "datum": datum,
                        "prio": prio,
                        "lkw_id": lkw_id.strip(),
                        "projekt": projekt,
                        "material": material,
                        "depot": depot,
                        "gps_km_plan": gps_km_plan,
                        "erloes_plan": erloes_plan,
                    }
                ]
            )
            st.session_state.planning_df = pd.concat([st.session_state.planning_df, new_row], ignore_index=True)
            st.success("Planungseintrag gespeichert.")


def depot_form() -> None:
    st.subheader("Depot-Transaktionsjournal")
    with st.form("depot_form", clear_on_submit=True):
        c1, c2 = st.columns(2)
        datum = c1.date_input("Buchungsdatum")
        depot = c2.selectbox("Depot", ["Lager 1", "Lager 2"])

        c3, c4, c5 = st.columns(3)
        material = c3.text_input("Material (Journal)")
        art = c4.selectbox("Art", ["Eingang", "Ausgang"])
        menge_t = c5.number_input("Menge (t)", min_value=0.0, step=10.0)

        submitted = st.form_submit_button("Buchung speichern")
        if submitted:
            st.session_state.journal_df = pd.concat(
                [
                    st.session_state.journal_df,
                    pd.DataFrame(
                        [{"datum": datum, "depot": depot, "material": material, "art": art, "menge_t": menge_t}]
                    ),
                ],
                ignore_index=True,
            )
            st.success("Depot-Buchung gespeichert.")


def show_dispatcher(real_df: pd.DataFrame, plan_df: pd.DataFrame, stock_df: pd.DataFrame, kritische_schwelle: float) -> None:
    st.header("Dispatcher-View")
    st.caption("Sicht auf Planung, Real-KM und Lagerstände (ohne sensitive Kostendaten).")

    if not plan_df.empty:
        dispatcher_df = plan_df.merge(real_df[["datum", "lkw_id", "gps_km_real"]], on=["datum", "lkw_id"], how="left")
        dispatcher_df["gps_km_real"] = dispatcher_df["gps_km_real"].fillna(0.0)
        dispatcher_df["delta_km"] = dispatcher_df["gps_km_plan"] - dispatcher_df["gps_km_real"]
        st.dataframe(dispatcher_df, use_container_width=True)
    else:
        st.info("Noch keine Planungsdaten vorhanden.")

    st.subheader("Lagerbestände")
    st.dataframe(stock_df[["depot", "bestand_t"]], use_container_width=True)

    alerts = stock_df[stock_df["bestand_t"] < kritische_schwelle]
    if not alerts.empty:
        for _, row in alerts.iterrows():
            st.error(f"⚠️ Kritischer Bestand in {row['depot']}: {row['bestand_t']:.1f} t (< {kritische_schwelle:.0f} t)")


def show_controller(controlling_df: pd.DataFrame) -> None:
    st.header("Controller-View")
    st.caption("Voller Zugriff auf Kosten- und DB-Analysen.")
    if controlling_df.empty:
        st.info("Noch keine verknüpften Plan-/Real-Daten vorhanden.")
        return

    kpi1, kpi2, kpi3 = st.columns(3)
    kpi1.metric("Gesamtkosten", f"{controlling_df['gesamtkosten'].sum():,.2f} €")
    kpi2.metric("Gesamt-DB", f"{controlling_df['deckungsbeitrag'].sum():,.2f} €")
    kpi3.metric("Ø Delta KM", f"{controlling_df['delta_km'].mean():,.2f}")

    st.dataframe(
        controlling_df[
            [
                "datum",
                "lkw_id",
                "projekt",
                "erloes_plan",
                "gps_km_plan",
                "gps_km_real",
                "delta_km",
                "fixkosten",
                "variable_kosten",
                "kraftstoffkosten",
                "gesamtkosten",
                "deckungsbeitrag",
            ]
        ],
        use_container_width=True,
    )


def main() -> None:
    st.set_page_config(page_title="Hammertaler Baustoffe - Controlling", layout="wide")
    st.title("🚛 Hammertaler Baustoffe - Automatisiertes Controlling")

    init_state()

    with st.sidebar:
        st.subheader("Setup")
        fixkosten = st.number_input("Fixkosten pro Tag (€)", min_value=0.0, value=250.0, step=10.0)
        km_satz = st.number_input("Variabler KM-Satz (€)", min_value=0.0, value=0.55, step=0.01)
        dieselpreis = st.number_input("Dieselpreis (€/l)", min_value=0.0, value=1.68, step=0.01)
        startbestand_1 = st.number_input("Startbestand Lager 1 (t)", min_value=0.0, value=2000.0, step=50.0)
        startbestand_2 = st.number_input("Startbestand Lager 2 (t)", min_value=0.0, value=1700.0, step=50.0)
        kritische_schwelle = st.number_input("Warnschwelle Bestand (t)", min_value=0.0, value=500.0, step=50.0)

        st.markdown("---")
        st.subheader("Webfleet PDF Upload")
        pdf_file = st.file_uploader("Tour-Report (PDF)", type=["pdf"])

    planning_form()
    depot_form()

    real_df = pd.DataFrame(
        columns=[
            "datum",
            "lkw_id",
            "gps_km_real",
            "kraftstoff_l",
            "stillstand_h",
            "start_position",
            "end_position",
        ]
    )

    if pdf_file is not None:
        real_df = parse_webfleet_pdf(pdf_file.read())
        st.subheader("Importierte Webfleet-Daten")
        st.dataframe(real_df, use_container_width=True)

    plan_df = st.session_state.planning_df.copy()
    if not plan_df.empty:
        plan_df["datum"] = pd.to_datetime(plan_df["datum"], errors="coerce").dt.date

    journal_df = st.session_state.journal_df.copy()
    _, stock_df = compute_inventory(journal_df, startbestand_1=startbestand_1, startbestand_2=startbestand_2)

    setup = CostSetup(fixkosten_pro_tag=fixkosten, variabler_km_satz=km_satz, dieselpreis_pro_l=dieselpreis)
    controlling_df = compute_costing(real_df=real_df, planning_df=plan_df, setup=setup) if not plan_df.empty else pd.DataFrame()

    st.markdown("---")
    tab1, tab2 = st.tabs(["Dispatcher", "Controller"])
    with tab1:
        show_dispatcher(real_df=real_df, plan_df=plan_df, stock_df=stock_df, kritische_schwelle=kritische_schwelle)
    with tab2:
        show_controller(controlling_df)


if __name__ == "__main__":
    main()
