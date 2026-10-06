from __future__ import annotations

from fintech_copilot.orchestration.models import Report

ACTION_TEXT = {
    "prepare_liquidity_buffer": "Подготовить резерв ликвидности",
    "review_operating_spend": "Проверить структуру операционных расходов",
    "review_collection_terms": "Проверить сроки оплаты клиентами",
    "verify_payment_documents": "Проверить назначение платежей и документы",
    "review_tax_treatment": "Передать расходы на налоговую проверку",
    "monitor_cash": "Продолжать мониторинг денежных потоков",
}
FINDING_TEXT = {
    "low_runway": "Ограниченный runway",
    "cash_gap": "Исторические кассовые разрывы",
    "opex_concentration": "Концентрация OPEX",
    "cashflow_anomaly": "Аномалия денежного потока",
    "unclassified_cross_border": "Неклассифицированные трансграничные платежи",
    "tax_review_required": "Расходы требуют налоговой проверки",
    "no_material_findings": "Существенные сигналы риска не обнаружены",
}


def escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").replace("<", "&lt;")


def markdown(report: Report) -> str:
    lines = [
        f"# {escape(report.company.name)}",
        "",
        f"Период: {report.period_start} — {report.period_end} (конец исключён).",
        f"Режим: {report.provider_mode}; статус: {report.status}.",
        "",
    ]
    if report.partial_reasons:
        lines.extend(["Причины неполного отчёта: " + "; ".join(report.partial_reasons), ""])
    facts = {(f.tool_result_id, f.fact_id): f for f in report.facts}
    if report.synthesis:
        for insight in report.synthesis.insights:
            lines.extend(
                [
                    f"## {FINDING_TEXT[insight.finding_code]}",
                    "",
                    ACTION_TEXT[insight.action_code] + ".",
                    "",
                    "| Факт | Значение | Источник / транзакции |",
                    "|---|---|---|",
                ]
            )
            for reference in insight.evidence:
                fact = facts[(reference.tool_result_id, reference.fact_id)]
                if fact.value is None:
                    value = "нет значения"
                elif isinstance(fact.value, tuple):
                    value = ", ".join(fact.value) or "пусто"
                else:
                    value = str(fact.value)
                evidence = ", ".join(fact.transaction_ids) or f"snapshot {report.snapshot_id}"
                lines.append(
                    f"| {fact.metric} | {escape(value)} {fact.unit} | "
                    f"`{fact.tool_result_id}` / {evidence} |"
                )
            lines.append("")
    lines.extend(
        [
            "## Налоговый драфт",
            "",
            "Expense review schedule; filing_ready=false. Налог и assessable profit не рассчитаны.",
            "",
        ]
    )
    if report.tax_draft:
        lines.extend(["| Классификация | HKD | Источник |", "|---|---|---|"])
        for fact in report.facts:
            if fact.metric.startswith("tax:") and fact.metric.endswith(":total"):
                lines.append(f"| {fact.metric} | {fact.value} | `{fact.tool_result_id}` |")
        lines.extend(["", "Недостающие данные: " + "; ".join(report.tax_draft.missing_inputs), ""])
    else:
        lines.extend(["Налоговый инструмент не завершён за полный период.", ""])
    lines.extend(["## Ограничения", ""] + [f"- {item}" for item in report.limitations])
    return "\n".join(lines) + "\n"
