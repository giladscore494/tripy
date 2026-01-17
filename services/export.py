from datetime import datetime, timedelta
from importlib import resources
from typing import Dict

from fpdf import FPDF
from ics import Calendar, Event


def _set_font(pdf: FPDF):
    try:
        with resources.path("fpdf.fonts", "DejaVuSans.ttf") as font_path:
            pdf.add_font("DejaVu", "", font_path, uni=True)
            pdf.set_font("DejaVu", size=12)
            return
    except Exception:
        # Fallback to core font; may limit glyphs but avoids crash
        pdf.set_font("Helvetica", size=12)


def itinerary_to_pdf(itinerary: Dict) -> bytes:
    if not isinstance(itinerary, dict):
        raise ValueError("itinerary must be a dict")
    pdf = FPDF()
    pdf.add_page()
    _set_font(pdf)

    def write_line(text: str):
        pdf.multi_cell(0, 10, txt=text)

    summary = itinerary.get("trip_summary") if isinstance(itinerary.get("trip_summary"), dict) else {}
    write_line(f"Trip to {summary.get('destination', 'Destination')}")
    write_line(f"Days: {summary.get('days', 'N/A')} | Pace: {summary.get('pace', 'N/A')}")
    write_line("Highlights:")
    for focus in summary.get("high_level_focus", []):
        write_line(f"- {focus}")

    pdf.ln(4)
    write_line("Lodging:")
    lodging_list = itinerary.get("lodging") if isinstance(itinerary.get("lodging"), list) else []
    for lodging in lodging_list:
        if not isinstance(lodging, dict):
            write_line(f"- {lodging}")
            continue
        write_line(
            f"- {lodging.get('name')} ({lodging.get('type')}, {lodging.get('area')}): "
            f"{lodging.get('why')}"
        )

    pdf.ln(4)
    day_entries = itinerary.get("days") if isinstance(itinerary.get("days"), list) else []
    for day in day_entries:
        if not isinstance(day, dict):
            continue
        write_line(f"Day {day.get('day')}: {day.get('theme', '')}")
        blocks = day.get("blocks") if isinstance(day.get("blocks"), list) else []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            write_line(
                f"  {block.get('time','').title()}: {block.get('activity')} "
                f"({block.get('area')}) - {block.get('duration_est')} | Transit: {block.get('transit_note')}"
            )
        foods = day.get("food") if isinstance(day.get("food"), list) else []
        for food in foods:
            if not isinstance(food, dict):
                write_line(f"  Food: {food}")
                continue
            write_line(
                f"  Food: {food.get('name')} ({food.get('type')}, {food.get('budget')} budget, {food.get('area')})"
            )
        plan_b_items = day.get("plan_b") if isinstance(day.get("plan_b"), list) else []
        for plan_b in plan_b_items:
            if not isinstance(plan_b, dict):
                write_line(f"  Plan B: {plan_b}")
                continue
            write_line(f"  Plan B ({plan_b.get('reason')}): {plan_b.get('alternative')}")
        pdf.ln(2)

    assumptions = itinerary.get("assumptions") if isinstance(itinerary.get("assumptions"), list) else []
    if assumptions:
        pdf.ln(4)
        write_line("Assumptions:")
        for a in assumptions:
            write_line(f"- {a}")
    disclaimer = itinerary.get("disclaimer") if isinstance(itinerary.get("disclaimer"), str) else ""
    if disclaimer:
        pdf.ln(4)
        write_line(f"Disclaimer: {disclaimer}")

    return pdf.output(dest="S").encode("latin-1")


def itinerary_to_ics(itinerary: Dict) -> bytes:
    if not isinstance(itinerary, dict):
        raise ValueError("itinerary must be a dict")
    cal = Calendar()
    start_date = datetime.utcnow().date()
    day_entries = itinerary.get("days") if isinstance(itinerary.get("days"), list) else []
    for day in day_entries:
        if not isinstance(day, dict):
            continue
        day_offset = day.get("day", 1) - 1
        base_date = start_date + timedelta(days=day_offset)
        times = {"morning": 9, "afternoon": 13, "evening": 19}
        blocks = day.get("blocks") if isinstance(day.get("blocks"), list) else []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            hour = times.get(block.get("time", "morning"), 9)
            event = Event()
            event.name = block.get("activity")
            event.begin = datetime.combine(base_date, datetime.min.time()) + timedelta(hours=hour)
            event.duration = timedelta(hours=2)
            event.location = block.get("area", "")
            event.description = block.get("notes", "")
            cal.events.add(event)
    return str(cal).encode("utf-8")
