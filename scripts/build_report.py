#!/usr/bin/env python3
"""Build the research PDF and data figures from versioned, local JSON reports."""
from __future__ import annotations
import argparse
import html
import json
import math
from pathlib import Path
import re
import shutil
import sys

import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (BaseDocTemplate, PageTemplate, Frame, Paragraph,
    Spacer, PageBreak, Table, TableStyle, Image, KeepTogether)
from reportlab.platypus.tableofcontents import TableOfContents

BASE=Path(__file__).resolve().parents[1]
DOCS=BASE/'docs'; FIGS=DOCS/'figures'
NAVY='#18364a'; TEAL='#078c83'; GRAY='#526777'; LIGHT='#edf4f5'


def font_setup():
    roots=[Path('/usr/share/fonts/truetype/open-sans'),Path('/usr/share/fonts/truetype/dejavu'),
           Path('C:/Windows/Fonts'),Path('/Library/Fonts'),BASE/'docs/fonts']
    candidates=[('OpenSans-Regular.ttf','OpenSans-Bold.ttf','OpenSans-Italic.ttf'),
                ('DejaVuSans.ttf','DejaVuSans-Bold.ttf','DejaVuSans-Oblique.ttf'),
                ('arial.ttf','arialbd.ttf','ariali.ttf')]
    for root in roots:
        for names in candidates:
            if all((root/n).is_file() for n in names):
                for label,filename in zip(['NS','NS-Bold','NS-Italic'],names):
                    pdfmetrics.registerFont(TTFont(label,str(root/filename)))
                pdfmetrics.registerFontFamily('NS',normal='NS',bold='NS-Bold',italic='NS-Italic',boldItalic='NS-Bold')
                return
    raise RuntimeError('Install Open Sans or DejaVu Sans (Cyrillic font required).')


def clean_math(s):
    replacements={r'\int_a^b':'integral(a,b)',r'\overline I':'mean(I)',r'\operatorname{mean}':'mean',
                  r'\sqrt':'sqrt',r'\sum_i':'sum(i)',r'\sum_j':'sum(j)',r'\Delta':'Δ',r'\ln':'ln',
                  r'\(':'',r'\)':'',r'\[':'',r'\]':'',r'\,':' ',r'\ge':'≥',r'\le':'≤'}
    for a,b in replacements.items():s=s.replace(a,b)
    return s


def inline(s):
    s=html.escape(clean_math(str(s)))
    s=re.sub(r'\*\*(.+?)\*\*',r'<b>\1</b>',s)
    s=re.sub(r'`([^`]+)`',r'<font color="#1b7080">\1</font>',s)
    s=re.sub(r'\[([^\]]+)\]\((https?://[^)]+)\)',r'<link color="#078c83" href="\2">\1</link>',s)
    return s


def styles():
    return {
      'body':ParagraphStyle('Body',fontName='NS',fontSize=9.2,leading=14.3,textColor=colors.HexColor(NAVY),spaceAfter=7),
      'small':ParagraphStyle('Small',fontName='NS',fontSize=7.7,leading=11.5,textColor=colors.HexColor(GRAY),spaceAfter=6),
      'h1':ParagraphStyle('H1',fontName='NS-Bold',fontSize=21,leading=27,textColor=colors.HexColor(NAVY),spaceBefore=13,spaceAfter=12,keepWithNext=True),
      'h2':ParagraphStyle('H2',fontName='NS-Bold',fontSize=13,leading=18,textColor=colors.HexColor(TEAL),spaceBefore=15,spaceAfter=8,keepWithNext=True),
      'h3':ParagraphStyle('H3',fontName='NS-Bold',fontSize=10.3,leading=15,textColor=colors.HexColor(NAVY),spaceBefore=10,spaceAfter=6,keepWithNext=True),
      'cell':ParagraphStyle('Cell',fontName='NS',fontSize=7.2,leading=10.5,textColor=colors.HexColor(NAVY)),
      'th':ParagraphStyle('TH',fontName='NS-Bold',fontSize=7.4,leading=11,textColor=colors.white),
      'cover':ParagraphStyle('Cover',fontName='NS-Bold',fontSize=39,leading=46,textColor=colors.HexColor(NAVY),spaceAfter=18),
      'lead':ParagraphStyle('Lead',fontName='NS',fontSize=14.5,leading=22,textColor=colors.HexColor(GRAY),spaceAfter=16),
      'bullet':ParagraphStyle('Bullet',fontName='NS',fontSize=9.2,leading=14.2,textColor=colors.HexColor(NAVY),leftIndent=11,firstLineIndent=-9,spaceAfter=5)
    }


class ReportDoc(BaseDocTemplate):
    def __init__(self,path):
        super().__init__(str(path),pagesize=A4,rightMargin=18*mm,leftMargin=18*mm,
                         topMargin=20*mm,bottomMargin=19*mm,
                         title='NeuroScope: исследование записей и научная основа анализа',
                         author='NeuroScope research',allowSplitting=1)
        frame=Frame(self.leftMargin,self.bottomMargin,self.width,self.height,id='normal',leftPadding=0,rightPadding=0,topPadding=0,bottomPadding=0)
        self.addPageTemplates(PageTemplate(id='report',frames=[frame],onPage=self.page_design))
        self._heading_count=0
    def beforeDocument(self):self._heading_count=0
    def page_design(self,c,d):
        c.saveState()
        if d.page>1:
            c.setFont('NS-Bold',8);c.setFillColor(colors.HexColor(TEAL));c.drawString(18*mm,285*mm,'NEUROSCOPE / RESEARCH NOTE')
        c.setFont('NS',7);c.setFillColor(colors.HexColor(GRAY))
        c.drawString(18*mm,11*mm,'11 ноября 2026 • Исследовательский анализ • Локальная обработка')
        c.drawRightString(192*mm,11*mm,str(d.page))
        c.restoreState()
    def afterFlowable(self,flowable):
        if isinstance(flowable,Paragraph) and flowable.style.name in ('H1','H2'):
            if self.page == 1 or flowable.getPlainText() == 'Содержание':
                return
            level=0 if flowable.style.name=='H1' else 1
            text=flowable.getPlainText();key=f'h{self._heading_count}';self._heading_count+=1
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(text,key,level=level,closed=False)
            if level==0:self.notify('TOCEntry',(level,text,self.page,key))


def table(rows, st, ratios=None):
    if not rows:return Spacer(1,0)
    n=len(rows[0]);width=174*mm
    ratios=ratios or [1]*n;total=sum(ratios)
    data=[[Paragraph(inline(cell),st['th'] if j==0 else st['cell']) for cell in row] for j,row in enumerate(rows)]
    t=Table(data,colWidths=[width*v/total for v in ratios],repeatRows=1,hAlign='LEFT')
    t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor(NAVY)),
      ('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),7),('RIGHTPADDING',(0,0),(-1,-1),7),
      ('TOPPADDING',(0,0),(-1,-1),7),('BOTTOMPADDING',(0,0),(-1,-1),7),
      ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#f1f6f7')]),
      ('LINEBELOW',(0,0),(-1,0),.6,colors.HexColor(TEAL)),
      ('LINEBELOW',(0,1),(-1,-1),.25,colors.HexColor('#dce7ec'))]))
    return t


def markdown(text,st,skip_title=False):
    story=[];lines=text.splitlines();i=0
    while i<len(lines):
        l=lines[i].strip()
        if not l:i+=1;continue
        if l.startswith('|'):
            rows=[]
            while i<len(lines) and lines[i].strip().startswith('|'):
                row=[v.strip() for v in lines[i].strip().strip('|').split('|')]
                if not all(re.fullmatch(r'[-: ]+',x or ' ') for x in row):rows.append(row)
                i+=1
                # Blank lines inside the source matrix are allowed.
                if i+1<len(lines) and not lines[i].strip() and lines[i+1].strip().startswith('|'):i+=1
            if rows:
                n=len(rows[0]);rows=[r[:n]+['']*max(0,n-len(r)) for r in rows]
                story.extend([table(rows,st,[1.1,1.1,1.3,1.4,1.2] if n==5 else None),Spacer(1,9)])
            continue
        m=re.match(r'^(#{1,4})\s+(.+)',l)
        if m:
            level=len(m[1]);i+=1
            if level==1 and skip_title:continue
            style='h1' if level==1 else 'h2' if level==2 else 'h3'
            story.append(Paragraph(inline(m[2]),st[style]));continue
        if l.startswith('```'):
            block=[];i+=1
            while i<len(lines) and not lines[i].startswith('```'):block.append(lines[i]);i+=1
            i+=1
            for line in block:story.append(Paragraph(inline(line),st['small']))
            continue
        if re.match(r'^[-*] ',l) or re.match(r'^\d+\. ',l):
            mark='• ' if l[:2] in ('- ','* ') else ''
            text=l[2:] if mark else l
            story.append(Paragraph(mark+inline(text),st['bullet']));i+=1;continue
        paragraph=[l];i+=1
        while i<len(lines) and lines[i].strip() and not re.match(r'^(#|\||[-*] |\d+\. |```)',lines[i].strip()):
            paragraph.append(lines[i].strip());i+=1
        paragraph_text=' '.join(paragraph)
        item=Paragraph(inline(paragraph_text),st['body'])
        if re.fullmatch(r'\*\*.+\*\*',paragraph_text):
            item.keepWithNext=True
        story.append(item)
    return story


def numeric(v,d=2):
    if v is None:return 'не определено'
    try:return f'{float(v):.{d}f}'.replace('.',',')
    except:return str(v)


def draw_figures(reports):
    FIGS.mkdir(parents=True,exist_ok=True)
    plt.rcParams.update({'font.family':'DejaVu Sans','font.size':9,'axes.spines.top':False,'axes.spines.right':False,
       'axes.labelcolor':NAVY,'text.color':NAVY,'xtick.color':GRAY,'ytick.color':GRAY,'axes.edgecolor':'#c3d0d9',
       'figure.facecolor':'white','axes.grid':True,'grid.alpha':.18,'axes.titleweight':'bold','savefig.dpi':170})
    paths={}
    for a in reports:
        sid=a['session']['id'];chs=a['eeg']['channels'];paths[sid]=[]
        if chs:
            fig,axes=plt.subplots(2,len(chs),figsize=(10,6.3),squeeze=False)
            for j,ch in enumerate(chs):
                psd=ch['psd'];f=np.array(psd['freq_hz']);raw=np.array(psd['raw_power'],float);screen=np.array(psd['power'],float)
                ax=axes[0,j];ax.semilogy(f,raw,color='#9cacc0',lw=1,label='До скрининга')
                if np.isfinite(screen).any():ax.semilogy(f,screen,color=TEAL,lw=1.3,label='После скрининга')
                ax.axvspan(48,52,color='#eaa46a',alpha=.25);ax.set_xlim(1,100);ax.set_xlabel('Частота, Гц')
                ax.set_ylabel('Исходная единица²/Гц');ax.set_title(ch['name']);ax.legend(fontsize=7)
                ex=ch['excerpt'];t=np.array(ex['time_s']);v=np.array(ex['values'],float)
                ax=axes[1,j];ax.step(t,v,where='mid',color=TEAL,lw=.9)
                ax.xaxis.set_major_locator(MaxNLocator(nbins=4,integer=True))
                ax.ticklabel_format(axis='x',style='plain',useOffset=False)
                ax.set_xlabel('Время от начала EEG, с');ax.set_ylabel('Исходная единица SDK');ax.set_title('Сохранённый фрагмент, без сглаживания',fontsize=9)
            fig.suptitle('Спектр и цифровой сигнал · '+a['session']['started_at'][:16].replace('T',' '),fontsize=12,y=1.02)
            fig.tight_layout();fp=FIGS/(sid+'_eeg.png');fig.savefig(fp,bbox_inches='tight');plt.close(fig);paths[sid].append((fp,'Серая линия — все конечные окна; бирюзовая — прошедшие скрининг. Оранжевая зона — 48–52 Гц. Фрагмент выбран по типичному RMS среди доступных окон. Единицы SDK не подтверждены.'))
            fig,axes=plt.subplots(2,1,figsize=(10,4.7),sharex=True)
            for j,ch in enumerate(chs):
                line=ch['timeline'];t=np.array([p['time_s']/60 for p in line]);alpha=[(p.get('bands') or {}).get('alpha') if p['usable'] else None for p in line]
                vals=np.array([np.nan if v is None else v*100 for v in alpha])
                axes[0].plot(t,vals,color=[TEAL,'#7b78c9'][j],lw=.7,label=ch['name'])
                good=np.array([p['usable'] for p in line]);axes[1].scatter(t,np.full(len(t),j),c=np.where(good,TEAL,'#df8668'),s=7,marker='|')
            axes[0].set_ylabel('Доля 8–13 Гц, %\nв диапазоне 1–45 Гц');axes[0].legend(fontsize=8)
            axes[0].set_title('Только описательная мощность сохранённого ряда; пропуски не соединены',fontsize=10)
            axes[1].set_yticks(range(len(chs)),[c['name'] for c in chs]);axes[1].set_xlabel('Время от начала EEG, мин');axes[1].set_ylim(-.7,len(chs)-.3)
            fig.tight_layout();fp=FIGS/(sid+'_timeline.png');fig.savefig(fp,bbox_inches='tight');plt.close(fig);paths[sid].append((fp,'Бирюзовые отметки — окно прошло эвристики, терракотовые — исключено. Окна длиной 4 с перекрываются на 50%; доли окон не являются долями чистого времени. Эти линии не показывают концентрацию или состояние глаз.'))
        fig,axes=plt.subplots(2,1,figsize=(10,4.8),sharex=False)
        ppg=a.get('ppg',{});line=ppg.get('timeline',[])
        if line:
            t=[p['time_s']/60 for p in line];cand=[p.get('candidate_pulse_rate_bpm') for p in line];good=[p.get('pulse_rate_bpm') for p in line]
            axes[0].scatter(t,cand,c='#b9c4d1',s=10,label='Кандидаты, включая отклонённые');axes[0].scatter(t,good,c=TEAL,s=18,label='Принятые окна')
            axes[0].set_ylabel('Оценка пульса, уд/мин');axes[0].set_xlabel('Время от начала PPG, мин');axes[0].legend(fontsize=7)
        for j,c in enumerate(a.get('impedance',{}).get('channels',[])):
            li=c.get('timeline',[]);axes[1].plot([v['time_s']/60 for v in li],[np.nan if v['value_ohm'] is None else v['value_ohm']/1000 for v in li],color=[TEAL,'#7b78c9'][j%2],lw=.8,label=c['name'])
        if a.get('impedance',{}).get('available'):
            axes[1].axhline(1000,ls='--',lw=.8,color='#c57655',label='Проектный порог 1 МОм');axes[1].legend(fontsize=7)
        else:axes[1].text(.5,.5,'Данные контакта отсутствуют',transform=axes[1].transAxes,ha='center')
        axes[1].set_ylabel('Контакт, кОм');axes[1].set_xlabel('Время от начала доступных потоков, мин')
        fig.suptitle('Пульс и контакт · '+a['session']['started_at'][:16].replace('T',' '),fontsize=12,y=1.02)
        fig.tight_layout();fp=FIGS/(sid+'_peripheral.png');fig.savefig(fp,bbox_inches='tight');plt.close(fig);paths[sid].append((fp,'Оценка пульса основана только на принятых 30-секундных окнах. Данные сопротивления показаны в прореженном виде; порог 1 МОм взят из конфигурации приложения, это не универсальная норма сухих EEG-электродов.'))
    return paths


def main():
    font_setup();DOCS.mkdir(exist_ok=True);st=styles()
    reports=[json.loads(p.read_text()) for p in sorted((BASE/'reports').glob('session_*.json'))]
    if len(reports)==0:raise RuntimeError('Сначала запустите analysis_engine.py --all')
    figs=draw_figures(reports);story=[]
    story.append(Spacer(1,23*mm))
    story.append(Paragraph('NEUROSCOPE',ParagraphStyle('Brand',fontName='NS-Bold',fontSize=13,leading=18,textColor=colors.HexColor(TEAL),spaceAfter=20)))
    story.append(Paragraph('Что говорят<br/>эти записи',st['cover']))
    story.append(Paragraph('ЭЭГ, пульс и движение.<br/>Глубокий разбор данных, научная основа<br/>и обновлённое приложение.',st['lead']))
    story.append(Spacer(1,12*mm))
    story.append(table([['3 сессии','2 отведения ЭЭГ','25 источников'],['24 и 27 октября 2026','O1–T3 / O2–T4','Первичные исследования и официальная документация']],st))
    story.append(Spacer(1,12*mm))
    story.append(Paragraph('Главный результат',st['h2']))
    story.append(Paragraph('Архив позволяет подробно описать качество и цифровые свойства сигналов, отдельные пульсовые показатели и выходы SDK. Он не позволяет достоверно оценить интеллект, личность, диагнозы или работу отдельных глубоких структур мозга. Главные ограничения — округление, помеха 50 Гц, происхождение времени и отсутствие разметки условий.',st['body']))
    story.append(Spacer(1,9*mm))
    story.append(Paragraph('Анализ выполнен 11 ноября 2026 года. Записи сохранены неизменными. Версия расчётного метода: '+reports[-1]['analysis_version']+'.',st['small']))
    story.append(PageBreak())
    story.append(Paragraph('Содержание',st['h1']));toc=TableOfContents()
    toc.levelStyles=[ParagraphStyle('TOC0',fontName='NS',fontSize=10,leading=17,leftIndent=0,spaceBefore=7)]
    story.append(toc);story.append(PageBreak())
    data_path=DOCS/'DATA_FINDINGS.md'
    if data_path.exists():story.extend(markdown(data_path.read_text(),st))
    else:raise RuntimeError('docs/DATA_FINDINGS.md is required')
    story.append(PageBreak());story.append(Paragraph('Графики: данные и качество',st['h1']))
    for a in reports:
        story.append(Paragraph(a['session']['started_at'][:16].replace('T',' ')+' · '+a['session']['id'],st['h2']))
        for fp,caption in figs[a['session']['id']]:
            from PIL import Image as PILImage
            with PILImage.open(fp) as im:w,h=im.size
            picture=Image(str(fp),width=174*mm,height=174*mm*h/w)
            story.append(KeepTogether([picture,Paragraph(inline(caption),st['small'])]));story.append(Spacer(1,8))
        story.append(PageBreak())
    research=DOCS/'RESEARCH.md'
    story.extend(markdown(research.read_text(),st));story.append(PageBreak())
    story.extend(markdown((DOCS/'APP_REVIEW.md').read_text(),st));story.append(PageBreak())
    story.append(Paragraph('Метод расчёта и воспроизводимость',st['h1']))
    exemplar=next((r for r in reports if r['eeg']['available']),reports[0])
    for key,text in exemplar['methods'].items():
        if isinstance(text,str):
            story.append(Paragraph(key,st['h2']));story.append(Paragraph(inline(text),st['body']))
    story.append(Paragraph('Конфигурация',st['h2']))
    story.append(table([['Параметр','Значение']]+[[k,str(v)] for k,v in exemplar['configuration'].items()],st,[2,1]))
    story.append(Paragraph('Целостность и повторение',st['h2']))
    story.append(Paragraph('Все исходные файлы в records/ включены без изменения байтов. Для каждого входа в JSON-отчёте указан SHA-256. Файл docs/INPUT_MANIFEST.json позволяет сопоставить входной ZIP и включённые записи. Текстовые выводы с численными значениями относятся к этой версии данных и параметров; изменение порогов создаёт новый результат, а не новую физиологическую истину.',st['body']))
    story.append(Paragraph('Команда пересчёта: python analysis_engine.py --all --records records --output reports. Команда проверки: python -m unittest discover -p test_*.py -v. Python 3.12; библиотеки и точные версии в requirements.txt.',st['body']))
    story.append(Paragraph('Пределы проверки',st['h2']))
    story.append(Paragraph('Синтетические тесты проверяют математические свойства алгоритмов и обработку некорректных входов; сравнение контрольных сумм проверяет сохранность источников. Это не клиническая валидация. Физическая повязка и нативный SDK здесь не подключались. Полный браузерный прогон ограничен средой выполнения; готовая автономная версия позволяет проверить интерфейс без запуска сервера.',st['body']))
    out=DOCS/'NeuroScope_Research_Report.pdf'
    ReportDoc(out).multiBuild(story)
    print(json.dumps({'pdf':str(out),'bytes':out.stat().st_size,'figures':sum(len(v) for v in figs.values())},ensure_ascii=False))

if __name__=='__main__':main()
