import json
import os

from openai import OpenAI

from ai_query_context import build_question_context


MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")


def ask_ai(question: str):
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY non configurata. Inserisci la chiave API nel file .env "
            "oppure come variabile di ambiente e riavvia il programma."
        )

    business_context = build_question_context(question)

    client = OpenAI(api_key=api_key)
    instructions = """
Sei l'assistente acquisti integrato nel gestionale AI Acquisti.

REGOLE OBBLIGATORIE:
1. Rispondi in italiano, in modo chiaro, concreto e sintetico.
2. Usa esclusivamente i dati presenti nel BUSINESS_CONTEXT.
3. Non inventare mai numeri, fornitori, prodotti, fatture, date o periodi.
4. BUSINESS_CONTEXT viene costruito con interrogazioni di sola lettura sul database aziendale.
5. Le statistiche indicate come full_history/full_database sono calcolate sull'intero database, non su un campione.
6. Per un prodotto usa latest_purchase_exact per dire quando, da chi e a che prezzo è stato acquistato l'ultima volta.
7. Per dire da quali fornitori è stato acquistato un prodotto usa all_suppliers_used_for_product.
8. Per sapere quali fornitori hanno il prodotto a listino usa latest_price_list_per_supplier: rappresenta l'ultimo listino disponibile per ciascun fornitore.
9. Distingui sempre prezzo di listino, sconti, prezzo netto di listino e prezzo realmente pagato.
10. Per minimo, massimo, media, totale e date usa purchase_statistics_full_history.
11. Per le anomalie usa anomaly_summary_full_history e recent_anomaly_details. Se price_variance_total è positivo è un maggior costo; se è negativo è un risparmio.
12. Se matched_products contiene più prodotti plausibili e la domanda non consente di distinguerli con sicurezza, chiedi quale prodotto intende l'utente invece di scegliere arbitrariamente.
13. Se non trovi un prodotto o un fornitore coerente con la domanda, dillo chiaramente e invita a indicare nome o codice articolo.
14. Gli elenchi marcati come recenti possono essere limitati per compattezza: non presentarli come esaustivi. Gli elenchi all_suppliers_used_for_product e latest_price_list_per_supplier invece sono completi per il prodotto individuato.
15. Non modificare dati e non affermare di aver eseguito azioni nel gestionale.
16. Non usare conoscenza esterna per completare dati aziendali mancanti.
"""

    user_input = (
        "BUSINESS_CONTEXT:\n"
        + json.dumps(business_context, ensure_ascii=False, default=str)
        + "\n\nDOMANDA DELL'UTENTE:\n"
        + question
    )

    response = client.responses.create(
        model=MODEL,
        instructions=instructions,
        input=user_input,
    )

    answer = (response.output_text or "").strip()
    if not answer:
        answer = "Non sono riuscito a generare una risposta dai dati disponibili."

    return {
        "answer": answer,
        "model": MODEL,
        "data_source": "database_azienda_completo",
        "query_mode": "targeted_read_only",
    }
