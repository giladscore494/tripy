from datetime import datetime, timedelta
from typing import Dict

from fpdf import FPDF
from ics import Calendar, Event


def itinerary_to_pdf(itinerary: Dict) -> bytes:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", size=12)

    def write_line(text: str):
        pdf.multi_cell(0, 10, txt=text)

    summary = itinerary.get("trip_summary", {})
    write_line(f"Trip to {summary.get('destination', 'Destination')}")
    write_line(f"Days: {summary.get('days', 'N/A')} | Pace: {summary.get('pace', 'N/A')}")
    write_line("Highlights:")
    for focus in summary.get("high_level_focus", []):
        write_line(f"- {focus}")

    pdf.ln(4)
    write_line("Lodging:")
    for lodging in itinerary.get("lodging", []):
        write_line(
            f"- {lodging.get('name')} ({lodging.get('type')}, {lodging.get('area')}): "
            f"{lodging.get('why')}"
        )

    pdf.ln(4)
    for day in itinerary.get("days", []):
        write_line(f"Day {day.get('day')}: {day.get('theme', '')}")
        for block in day.get("blocks", []):
            write_line(
                f"  {block.get('time','').title()}: {block.get('activity')} "
                f"({block.get('area')}) - {block.get('duration_est')} | Transit: {block.get('transit_note')}"
            )
        for food in day.get("food", []):
            write_line(
                f"  Food: {food.get('name')} ({food.get('type')}, {food.get('budget')} budget, {food.get('area')})"
            )
        for plan_b in day.get("plan_b", []):
            write_line(f"  Plan B ({plan_b.get('reason')}): {plan_b.get('alternative')}")
        pdf.ln(2)

    if itinerary.get("assumptions"):
        pdf.ln(4)
        write_line("Assumptions:")
        for a in itinerary["assumptions"]:
            write_line(f"- {a}")
    if itinerary.get("disclaimer"):
        pdf.ln(4)
        write_line(f"Disclaimer: {itinerary['disclaimer']}")

    return pdf.output(dest="S").encode("latin-1")


def itinerary_to_ics(itinerary: Dict) -> bytes:
    cal = Calendar()
    start_date = datetime.utcnow().date()
    for day in itinerary.get("days", []):
        day_offset = day.get("day", 1) - 1
        base_date = start_date + timedelta(days=day_offset)
        times = {"morning": 9, "afternoon": 13, "evening": 19}
        for block in day.get("blocks", []):
            hour = times.get(block.get("time", "morning"), 9)
            event = Event()
            event.name = block.get("activity")
            event.begin = datetime.combine(base_date, datetime.min.time()) + timedelta(hours=hour)
            event.duration = timedelta(hours=2)
            event.location = block.get("area", "")
            event.description = block.get("notes", "")
            cal.events.add(event)
    return str(cal).encode("utf-8")
