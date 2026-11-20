"""File ingestion with explicit coverage and source coordinates."""
import hashlib
import io
import os
import shutil
import subprocess
import threading
import zipfile
from pathlib import Path

from pypdf import PdfReader
import pypdfium2 as pdfium

from store import DATA, db, execute, one

PDF_LOCK = threading.RLock()
ALLOWED = {'.pdf', '.docx', '.doc', '.xlsx', '.xls'}
MAX_SIZE = 300 * 1024 * 1024

def setting(name,default=''):
    if name in os.environ:return os.environ[name]
    from store import ROOT
    env=ROOT/'.env'
    if env.exists():
        for line in env.read_text(encoding='utf-8-sig').splitlines():
            if line.strip() and not line.lstrip().startswith('#') and '=' in line:
                key,value=line.split('=',1)
                if key.strip()==name:return value.strip().strip('"').strip("'")
    return default

def validate_file(path, name):
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED:
        raise ValueError('支持 PDF、DOCX、DOC、XLSX 和 XLS 文件')
    with open(path, 'rb') as f:
        header = f.read(1024)
    if ext == '.pdf' and b'%PDF-' not in header:
        raise ValueError('文件内容不是有效PDF')
    if ext in {'.doc', '.xls'} and not header.startswith(bytes.fromhex('D0CF11E0A1B11AE1')):
        raise ValueError('旧版Office文件格式不匹配')
    if ext in {'.docx', '.xlsx'}:
        with zipfile.ZipFile(path) as z:
            member = 'word/document.xml' if ext == '.docx' else 'xl/workbook.xml'
            if member not in z.namelist():
                raise ValueError('Office文件格式与扩展名不符')
            if sum(i.file_size for i in z.infolist()) > 1024 * 1024 * 1024:
                raise ValueError('Office解压内容过大')
    return ext

def save_page(file_id, number, label, text, quality='readable', note=''):
    with db() as c:
        c.execute('INSERT OR REPLACE INTO pages VALUES(?,?,?,?,?,?)',
                  (file_id, number, label, text, quality, note))
        c.execute('UPDATE files SET done=? WHERE id=?', (number, file_id))

def render_png(path, number, scale=1.35):
    with PDF_LOCK:
        with pdfium.PdfDocument(str(path)) as pdf:
            if not 1 <= number <= len(pdf):
                raise ValueError('页码超出范围')
            page = pdf[number-1]
            image = page.render(scale=scale).to_pil()
            out = io.BytesIO()
            image.save(out, format='PNG')
            image.close()
            page.close()
            return out.getvalue()

def ocr_page(path, number):
    exe = setting('TESSERACT_CMD') or shutil.which('tesseract')
    if not exe:
        return '', '未配置中文OCR，需人工核对原页'
    dest = DATA / 'previews' / f'ocr-{hashlib.md5(str(path).encode()).hexdigest()}-{number}.png'
    dest.write_bytes(render_png(path, number, scale=1.8))
    try:
        result = subprocess.run([exe, str(dest), 'stdout', '-l', setting('OCR_LANG','chi_sim+eng'), '--psm','3'],
                                capture_output=True, timeout=90)
        if result.returncode:
            return '', 'OCR失败或缺少中文语言包，需人工核对原页'
        return result.stdout.decode('utf-8',errors='replace').strip(), '本页经OCR识别，关键参数须对照原图'
    except subprocess.TimeoutExpired:
        return '', 'OCR超时，需人工核对原页'
    finally:
        dest.unlink(missing_ok=True)

def office_pdf(path, target):
    """Optional installed Office converter; never modifies source files."""
    soffice = shutil.which('soffice')
    if soffice:
        try:
            subprocess.run([soffice,'--headless','--convert-to','pdf','--outdir',str(target.parent),str(path)],
                           capture_output=True, timeout=120, check=True)
        except (OSError,subprocess.TimeoutExpired,subprocess.CalledProcessError):return False
        produced = target.parent / (path.stem+'.pdf')
        if produced != target and produced.exists():
            produced.replace(target)
        return target.exists()
    if os.name == 'nt':
        # Only generated absolute paths are passed as script parameters.
        script = DATA / 'previews' / 'office_convert.ps1'
        if not script.exists():
            script.write_text('''param([string]$Source,[string]$Target)
$ErrorActionPreference = 'Stop'
$app = New-Object -ComObject 'kwps.Application'
$app.Visible = $false
$app.DisplayAlerts = 0
try {
 $doc = $app.Documents.Open($Source,$false,$true)
 try { $doc.ExportAsFixedFormat($Target,17) } finally { $doc.Close(0) }
} finally { $app.Quit() }
''',encoding='utf-8-sig')
        try:
            result = subprocess.run(['powershell','-NoProfile','-ExecutionPolicy','Bypass','-File',str(script),str(path),str(target)],
                                    capture_output=True, timeout=120)
            return result.returncode == 0 and target.exists()
        except (OSError, subprocess.TimeoutExpired):
            return False
    return False

def parse_pdf(file_id, path):
    with PDF_LOCK:
        reader = PdfReader(str(path))
        try:
            if reader.is_encrypted:
                raise ValueError('加密PDF需先解密后上传')
            total = len(reader.pages)
            if total > 2000:
                raise ValueError('单文件超过2000页，请分册上传')
            execute('UPDATE files SET page_count=? WHERE id=?', (total,file_id))
            for i,page in enumerate(reader.pages,1):
                try:
                    text = page.extract_text() or ''
                    xobjects = page.get('/Resources',{}).get('/XObject',{})
                    scanned = any(x.get_object().get('/Subtype')=='/Image' for x in xobjects.values())
                    quality = 'needs_check' if scanned else 'readable'
                    note = '含扫描图像；文字层及技术参数需原页核验' if scanned else ''
                    if len(text.strip()) < 70:
                        recovered, ocr_note = ocr_page(path,i)
                        if len(recovered) > len(text):
                            text = recovered
                        quality = 'needs_check' if len(text.strip()) >= 70 else 'unreadable'
                        note = ocr_note
                    save_page(file_id,i,f'PDF第{i}页',text,quality,note)
                except Exception as exc:
                    save_page(file_id,i,f'PDF第{i}页','','unreadable','本页解析失败：'+str(exc)[:180])
                finally:
                    reader.resolved_objects.clear()
        finally:
            reader.close()

def parse_office(file_id,path,ext):
    preview = DATA / 'previews' / (file_id+'.pdf')
    if office_pdf(path,preview):
        parse_pdf(file_id,preview)
        return
    if ext == '.doc':
        raise ValueError('旧版DOC需要安装WPS或LibreOffice，或转换为DOCX/PDF后上传')
    from docx import Document
    doc = Document(path)
    groups=[]
    for idx,block in enumerate(doc.element.body,1):
        text='\n'.join(''.join(n.text or '' for n in p.iter() if n.tag.endswith('}t'))
            for p in block.iter() if p.tag.endswith('}p'))
        if text.strip():
            groups.append((idx,text.strip()))
    execute('UPDATE files SET page_count=? WHERE id=?',(len(groups),file_id))
    for n,(paragraph,text) in enumerate(groups,1):
        save_page(file_id,n,f'原文块{paragraph}',text,'needs_check','未安装固定版式转换器，按原文块定位；非物理页码')

def parse_excel(file_id,path,ext):
    groups=[]
    if ext == '.xlsx':
        import openpyxl
        book = openpyxl.load_workbook(path,read_only=True,data_only=False)
        for sheet in book:
            buffer=[]; first=1
            for n,row in enumerate(sheet.iter_rows(),1):
                values=['%s=%s'%(c.coordinate,str(c.value)) for c in row if c.value is not None]
                if values:
                    buffer.append(' | '.join(values))
                if n%40==0:
                    groups.append((f'{sheet.title}!行{first}-{n}','\n'.join(buffer)))
                    buffer=[]; first=n+1
            if buffer:
                groups.append((f'{sheet.title}!行{first}-{sheet.max_row}','\n'.join(buffer)))
        book.close()
    else:
        import xlrd
        book=xlrd.open_workbook(str(path),on_demand=True)
        for sheet in book.sheets():
            for start in range(0,sheet.nrows,40):
                lines=[f'行{n+1}: '+' | '.join(str(x) for x in sheet.row_values(n)) for n in range(start,min(start+40,sheet.nrows))]
                groups.append((f'{sheet.name}!行{start+1}-{min(start+40,sheet.nrows)}','\n'.join(lines)))
        book.release_resources()
    execute('UPDATE files SET page_count=? WHERE id=?',(len(groups),file_id))
    for n,(label,text) in enumerate(groups,1):
        save_page(file_id,n,label,text,'needs_check','清单按单元格或行定位；公式不执行，不替代计量审核')

def parse_file(file_id):
    row=one('SELECT * FROM files WHERE id=?',(file_id,))
    if not row:
        return
    execute("UPDATE files SET status='parsing',done=0,error='' WHERE id=?",(file_id,))
    try:
        path=Path(row['path']); ext=validate_file(path,row['name'])
        with db() as c:
            c.execute('DELETE FROM pages WHERE file_id=?',(file_id,))
        if ext=='.pdf': parse_pdf(file_id,path)
        elif ext in {'.doc','.docx'}: parse_office(file_id,path,ext)
        else: parse_excel(file_id,path,ext)
        pages=one('SELECT COUNT(*) n FROM pages WHERE file_id=?',(file_id,))['n']
        if not pages:
            raise ValueError('未解析到可用内容')
        execute("UPDATE files SET status='ready' WHERE id=?",(file_id,))
    except Exception as exc:
        execute("UPDATE files SET status='failed',error=? WHERE id=?",(str(exc)[:500],file_id))
