"""Build an up-to-date local PDF from current analyses, not a bundled artifact."""
from __future__ import annotations
import html
import io
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def generate_research_pdf(reports: list[dict[str, Any]], knowledge: dict) -> bytes:
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, KeepTogether, Table, TableStyle
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib import colors
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    font='Helvetica'; bold='Helvetica-Bold'
    for root in (Path('/usr/share/fonts/truetype/dejavu'), Path('/usr/share/fonts/truetype/open-sans')):
        names=('DejaVuSans.ttf','DejaVuSans-Bold.ttf') if 'dejavu' in str(root) else ('OpenSans-Regular.ttf','OpenSans-Bold.ttf')
        if all((root/name).is_file() for name in names):
            for alias,name in (('NSReport',names[0]),('NSReportBold',names[1])):
                if alias not in pdfmetrics.getRegisteredFontNames():
                    pdfmetrics.registerFont(TTFont(alias,str(root/name)))
            font,bold='NSReport','NSReportBold'
            break
    if font == 'Helvetica':
        raise RuntimeError('Для русского PDF установите шрифт DejaVu Sans или Open Sans')
    buf=io.BytesIO()
    doc=SimpleDocTemplate(buf,pagesize=A4,leftMargin=40,rightMargin=40,topMargin=43,bottomMargin=45,
                          title='NeuroScope — локальный исследовательский отчёт')
    base=ParagraphStyle('base',fontName=font,fontSize=9,leading=14,spaceAfter=8,textColor=colors.HexColor('#22344a'))
    h1=ParagraphStyle('title',parent=base,fontName=bold,fontSize=21,leading=27,spaceAfter=16)
    h2=ParagraphStyle('section',parent=base,fontName=bold,fontSize=13,leading=17,spaceBefore=12)
    small=ParagraphStyle('note',parent=base,fontSize=7.7,leading=12,textColor=colors.HexColor('#56677a'))
    escape=lambda x: html.escape(str(x if x is not None else '—'))
    story=[Paragraph('NeuroScope · Исследование записей',h1),
           Paragraph('Создан локально: '+datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),small),
           Paragraph('Это инженерно-исследовательское описание сохранённых сигналов, а не медицинское заключение. '
                     'Не определяет диагноз, интеллект, личность или активность отдельных структур мозга. '
                     'Физические единицы ЭЭГ в старых CSV не подтверждены.',base),
           Paragraph('Состав данных',h2),
           Paragraph(f'Проанализировано завершённых записей: {len(reports)}. Числа могут быть недоступны при низком качестве сигнала.',base)]
    for report in reports:
        s,q=report.get('session',{}),report.get('quality',{})
        title=escape(s.get('id','без идентификатора'))
        lines=[Paragraph(title,h2),
               Paragraph('Состояние качества: '+escape(q.get('status'))+'; ЭЭГ-окна без инженерных флагов: '+escape(q.get('usable_eeg_pct'))+'%.',base),
               Paragraph('Длительность по отсчётам: '+escape(s.get('duration_s'))+' с. Каналы: '+escape(s.get('channels'))+'.',base)]
        for item in (report.get('observations') or [])[:6]:
            lines.append(Paragraph('<b>'+escape(item.get('title'))+'</b>. '+escape(item.get('detail')),base))
        lines.append(Paragraph('Данные получены из локальных файлов; подробные условия фильтрации и исходные контрольные суммы содержатся в JSON-экспорте сессии.',small))
        story.extend(lines)
    if not reports:
        story.append(Paragraph('Нет завершённых сессий. Запишите данные и повторите экспорт.',base))
    sources=(knowledge or {}).get('sources',[])
    story.append(Paragraph('Методологические источники',h2))
    for row in sources[:15]:
        story.append(Paragraph('• '+escape(row.get('title') or row.get('id'))+' — '+escape(row.get('url')) ,small))
    story.append(Paragraph('Автоматический PDF отражает состояние файлов на момент запроса. '
                            'Он не содержит исходных отсчётов; для независимой проверки выгрузите CSV и JSON.',small))
    doc.build(story)
    return buf.getvalue()
