"""AI Chat endpoint — mock portfolio analysis (no LLM integration)."""

import time
from fastapi import APIRouter, HTTPException, Request
from deps import limiter
from validators import AIChatRequest
from utils import _safe_float

router = APIRouter(tags=["AI"])


@router.post("/ai/chat")
@limiter.limit("20/minute")
async def ai_chat(request: Request):
    """AI chat endpoint. Mock mode generates portfolio analysis without LLM."""
    body = await request.json()
    try:
        validated = AIChatRequest(**body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request body")
    message = validated.message.strip()
    positions = [p.dict() for p in validated.positions]

    if not message:
        raise HTTPException(status_code=400, detail="Message is required")

    # Build portfolio context
    tickers = [p.get("ticker", "") for p in positions if p.get("ticker")]
    total_value = sum(p.get("value", 0) for p in positions)
    total_cost = sum(p.get("cost", 0) for p in positions)
    pnl = total_value - total_cost
    pnl_pct = (pnl / total_cost * 100) if total_cost > 0 else 0
    position_count = len(positions)

    # Sort by value for top holdings
    sorted_positions = sorted(positions, key=lambda p: p.get("value", 0), reverse=True)
    top_5 = sorted_positions[:5]

    # Group by sector if available
    sectors = {}
    for p in positions:
        sector = p.get("sector", "Sin sector")
        sectors[sector] = sectors.get(sector, 0) + p.get("value", 0)

    # Generate mock analysis based on message keywords
    msg_lower = message.lower()

    sections = []

    # Always include portfolio summary
    sections.append(
        f"\U0001f4ca **Resumen del Portfolio**\n"
        f"- Total invertido: ${total_cost:,.2f}\n"
        f"- Valor actual: ${total_value:,.2f}\n"
        f"- P&L: {'+'if pnl>=0 else ''}{pnl:,.2f} ({pnl_pct:+.1f}%)\n"
        f"- Posiciones: {position_count}"
    )

    # Top holdings
    if top_5:
        holdings_text = "\n".join(
            f"  {i+1}. **{p.get('ticker', '?')}** \u2014 ${p.get('value', 0):,.0f} ({p.get('value', 0)/total_value*100:.1f}%)"
            for i, p in enumerate(top_5) if total_value > 0
        )
        sections.append(f"\U0001f3c6 **Top Holdings**\n{holdings_text}")

    # Sector distribution
    if sectors and len(sectors) > 1:
        sector_text = "\n".join(
            f"  - {s}: ${v:,.0f} ({v/total_value*100:.1f}%)"
            for s, v in sorted(sectors.items(), key=lambda x: x[1], reverse=True)[:6]
            if total_value > 0
        )
        sections.append(f"\U0001f4c8 **Distribuci\u00f3n Sectorial**\n{sector_text}")

    # Context-specific analysis based on keywords
    if any(w in msg_lower for w in ["diversif", "riesgo", "concentr"]):
        if len(positions) < 5:
            sections.append("\u26a0\ufe0f **Observaci\u00f3n sobre Diversificaci\u00f3n**\nTu portfolio tiene pocas posiciones. Considera diversificar en m\u00e1s sectores o geograf\u00edas para reducir el riesgo espec\u00edfico.")
        elif top_5 and total_value > 0 and (top_5[0].get("value", 0) / total_value) > 0.3:
            sections.append(f"\u26a0\ufe0f **Concentraci\u00f3n Detectada**\n{top_5[0].get('ticker')} representa m\u00e1s del 30% de tu portfolio. Una ca\u00edda significativa tendr\u00eda un impacto desproporcionado.")
        else:
            sections.append("\u2705 **Diversificaci\u00f3n**\nTu portfolio muestra una distribuci\u00f3n razonable entre posiciones.")

    if any(w in msg_lower for w in ["rendimiento", "performance", "retorno", "ganancia"]):
        if pnl > 0:
            sections.append(f"\U0001f4c8 **Rendimiento**\nTu portfolio est\u00e1 en positivo con un retorno del {pnl_pct:.1f}%. Las posiciones con mejor rendimiento son las de mayor valor actual.")
        else:
            sections.append(f"\U0001f4c9 **Rendimiento**\nTu portfolio muestra una p\u00e9rdida del {pnl_pct:.1f}%. Considera revisar las posiciones con peor rendimiento.")

    if any(w in msg_lower for w in ["qu\u00e9 opinas", "analiz", "general", "resumen", "hola", "ayuda"]):
        sections.append(
            "\U0001f4a1 **Sugerencias**\n"
            "- Revisa peri\u00f3dicamente tu asignaci\u00f3n de activos\n"
            "- Considera el impacto fiscal antes de vender\n"
            "- Mant\u00e9n un fondo de emergencia fuera del portfolio\n"
            "- La diversificaci\u00f3n geogr\u00e1fica puede reducir el riesgo"
        )

    sections.append("\n\u26a0\ufe0f *Este an\u00e1lisis es orientativo y no constituye asesoramiento financiero. Consulta con un profesional antes de tomar decisiones de inversi\u00f3n.*")

    response_text = "\n\n".join(sections)

    # B-006: drop the misleading `mode: "mock"` field. Surface a clear
    # `coming_soon: true` flag so the frontend can label the response as a
    # placeholder until real LLM integration lands.
    return {
        "role": "assistant",
        "content": response_text,
        "coming_soon": True,
        "timestamp": time.time(),
    }
