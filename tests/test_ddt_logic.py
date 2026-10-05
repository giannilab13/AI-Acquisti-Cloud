import unittest

from ddt_importer import (
    _normalise_ddt,
    _quality_reasons,
    build_quantity_checks,
    normalize_ddt_number,
)
from xml_importer import parse_invoice


class DdtLogicTests(unittest.TestCase):
    def test_photo_data_does_not_require_price(self):
        parsed = _normalise_ddt({
            "supplier": {"name": "Alfa Srl", "vat": "IT123"},
            "ddt_number": "DDT 25/2026",
            "ddt_date": "2026-10-05",
            "lines": [{
                "line_number": 1,
                "supplier_code": "ABC123",
                "description": "Filtro olio",
                "quantity": "5",
                "unit": "pz",
            }],
            "uncertain_fields": [],
        })
        self.assertEqual(_quality_reasons(parsed), [])
        self.assertEqual(parsed["lines"][0]["quantity"], 5.0)
        self.assertEqual(normalize_ddt_number(parsed["ddt_number"]), "DDT252026")

    def test_unreadable_quantity_requires_review(self):
        parsed = _normalise_ddt({
            "supplier": {"name": "Alfa Srl", "vat": ""},
            "ddt_number": "44",
            "ddt_date": "2026-10-05",
            "lines": [{"description": "Filtro aria", "quantity": 0}],
            "uncertain_fields": ["quantita riga 1"],
        })
        reasons = _quality_reasons(parsed)
        self.assertTrue(any("Quantita riga 1" in item for item in reasons))

    def test_missing_supplier_code_is_not_a_review_reason(self):
        parsed = _normalise_ddt({
            "supplier": {"name": "AD.EL s.r.l.", "vat": "02865880278"},
            "ddt_number": "736",
            "ddt_date": "2025-09-10",
            "lines": [{
                "description": "TELECOMANDI LOCKER LAUNDRY",
                "supplier_code": "",
                "quantity": 6,
                "unit": "pz",
            }],
            "uncertain_fields": ["lines[0].supplier_code"],
        })
        self.assertEqual(_quality_reasons(parsed), [])

    def test_fatturapa_extracts_ddt_references(self):
        xml = b"""<?xml version="1.0" encoding="UTF-8"?>
        <FatturaElettronica>
          <FatturaElettronicaHeader>
            <CedentePrestatore><DatiAnagrafici>
              <IdFiscaleIVA><IdPaese>IT</IdPaese><IdCodice>123</IdCodice></IdFiscaleIVA>
              <Anagrafica><Denominazione>Alfa Srl</Denominazione></Anagrafica>
            </DatiAnagrafici></CedentePrestatore>
          </FatturaElettronicaHeader>
          <FatturaElettronicaBody>
            <DatiGenerali>
              <DatiGeneraliDocumento>
                <TipoDocumento>TD01</TipoDocumento><Divisa>EUR</Divisa>
                <Data>2026-10-31</Data><Numero>F-100</Numero><ImportoTotaleDocumento>50</ImportoTotaleDocumento>
              </DatiGeneraliDocumento>
              <DatiDDT><NumeroDDT>25</NumeroDDT><DataDDT>2026-10-05</DataDDT></DatiDDT>
              <DatiDDT><NumeroDDT>31</NumeroDDT><DataDDT>2026-10-12</DataDDT></DatiDDT>
            </DatiGenerali>
            <DatiBeniServizi><DettaglioLinee>
              <NumeroLinea>1</NumeroLinea><Descrizione>Filtro olio</Descrizione>
              <Quantita>5</Quantita><UnitaMisura>PZ</UnitaMisura>
              <PrezzoUnitario>10</PrezzoUnitario><PrezzoTotale>50</PrezzoTotale><AliquotaIVA>22</AliquotaIVA>
            </DettaglioLinee></DatiBeniServizi>
          </FatturaElettronicaBody>
        </FatturaElettronica>"""
        parsed = parse_invoice(xml)
        self.assertEqual(parsed["delivery_notes"], [
            {"number": "25", "date": "2026-10-05"},
            {"number": "31", "date": "2026-10-12"},
        ])

    def test_quantity_comparison_finds_all_difference_types(self):
        checks = build_quantity_checks(
            {"P1": 10, "P2": 5, "P3": 4},
            {"P1": 10, "P2": 3, "P4": 2},
        )
        statuses = {item["product_id"]: item["status"] for item in checks}
        self.assertEqual(statuses, {
            "P1": "OK",
            "P2": "QUANTITA_DIFFERENTE",
            "P3": "SOLO_FATTURA",
            "P4": "SOLO_DDT",
        })


if __name__ == "__main__":
    unittest.main()
