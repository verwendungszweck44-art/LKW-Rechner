# Hammertaler Baustoffe – LKW Controlling Tool

Streamlit-basierte Lösung zum Abgleich von **Planungsdaten** und **Webfleet-Realwerten** inkl. Kostenrechnung und Depotmanagement.

## Features

- **PDF-Parser (Webfleet Tour-Reports)**
  - liest PDF-Tabellen via `pdfplumber`
  - filtert Fahrzeuge mit Präfix `WIT-HB`
  - extrahiert Datum, KM, Liter, Stillstand, Start-/Endposition
  - bereinigt numerische Werte (`km`, `l`, Dezimaltrennzeichen)
- **Controlling-Engine**
  - Fixkosten pro Tag
  - variable KM-Kosten
  - Kraftstoffkosten auf Basis Dieselpreis
  - Deckungsbeitrag (Erlös Plan - Gesamtkosten Real)
  - Delta zwischen geplanten KM und GPS-KM
- **Depot-Management**
  - Transaktionsjournal (`Eingang`/`Ausgang`)
  - Bestandsberechnung pro Lager
  - Warnung bei kritischer Schwelle (z. B. 500 t)
- **Rollennahe UI**
  - Dispatcher-View: Planung + Lager + KM-Abweichung (ohne Kosten)
  - Controller-View: vollständige Kosten-/DB-Transparenz

## Start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

## Hinweise zur Datenstruktur

### Planungsmaske
Erwartete Felder:
- Datum
- Prio
- LKW-ID
- Projekt
- Material
- Depot
- Geplante KM
- Erlös Plan

Für Projekte mit "Spiekermann" wird bei leerem Erlös ein Sonderpreis-Default von `650 €` gesetzt.

### Depotjournal
- Buchungsdatum
- Depot (`Lager 1` / `Lager 2`)
- Material
- Art (`Eingang` / `Ausgang`)
- Menge in Tonnen

## Erweiterungsideen

- robustere PDF-Mappings je nach Webfleet-Layoutvariante
- persistente Speicherung (PostgreSQL/SQLite)
- SSO + rollenbasierte Authentifizierung
- automatisierter PDF-Inbox-Import (E-Mail/SharePoint)
